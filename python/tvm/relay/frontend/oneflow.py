import os
import copy
import warnings

import numpy as np
import tvm
from tvm.ir import IRModule
from tvm.relay.analysis.analysis import check_basic_block_normal_form
from tvm.topi.utils import get_const_tuple

from ... import nd as _nd
from .. import analysis
from .. import expr as _expr
from .. import function as _function
from .. import loops as _loops
from .. import op as _op
from .. import qnn as _qnn
from .. import ty as _ty
from .. import vision as _vision
from .common import (
    AttrCvt,
    Renamer,
    fold_constant,
    get_name,
    get_relay_op,
    infer_channels,
    infer_shape,
    infer_type,
    infer_value,
    new_var,
)

__all__ = ["from_oneflow"]

FLOW_2_NP_DTYPE = {
    2: np.float32,
    3: np.float64,
    6: np.int64,
    5: np.int32,
    4: np.int8,
    7: np.uint8,
    9: np.float16
}

NP_2_TVM_DTYPE = {
    np.float16: "float16",
    np.float32: "float32",
    np.float64: "float64",
    np.int64: "int64",
    np.int32: "int32",
    np.int8: "int8",
    np.uint8: "uint8",
}

_identity_list = []


def is_input_op(node):
    # 用来判断该节点的op是否为input_conf
    return node.WhichOneof("op_type") == "input_conf"


def is_user_op(node):
    # 用来判断该节点的op是否为user_conf
    return node.WhichOneof("op_type") == "user_conf"


def is_output_op(node):
    # 用来判断该节点的op是否为return_conf
    return node.WhichOneof("op_type") == "return_conf"


def is_param_op(node):
    # 用来判断该节点的op是否为variable_conf
    return node.WhichOneof("op_type") == "variable_conf"


def get_node_info(node):
    """
    获取node基本信息: shape、data_type
    """
    # 获取形状，转为list->tuple
    shape = tuple(node.input_conf.blob_conf.shape.dim)
    # 获取数据类型
    dtype = node.input_conf.blob_conf.data_type
    if dtype in list(FLOW_2_NP_DTYPE.keys()):
        data_type = FLOW_2_NP_DTYPE[dtype]
    else:
        raise IndexError('Please check the data type of your node: %s' % node.name)
    return shape, data_type


def parse_attr(attr):
    # 解析node_attr
    # TODO: 可能数据类型有遗漏
    attrs = {}
    for a in attr:
        attr_str = str(attr[a])

        if attr_str[0:7] == "at_list":
            attr_str_ = attr_str.split(" ")[0]

            if attr_str_ == "at_list_float":
                attrs[a] = tuple(attr[a].at_list_float.val)
            elif attr_str_ == "at_list_int32":
                attrs[a] = tuple(attr[a].at_list_int32.val)
            elif attr_str_ == "at_list_int64":
                attrs[a] = tuple(attr[a].at_list_int64.val)

        elif attr_str.split(":")[0] == "at_string":
            attrs[a] = attr[a].at_string

        elif attr_str.split(" ")[0] == "at_shape":
            attrs[a] = tuple(list(attr[a].at_shape.dim))

        else:
            attr_str_ = attr_str.split(":")[0]
            if attr_str_ == "at_bool":
                attrs[a] = attr[a].at_bool
            elif attr_str_ == "at_double":
                attrs[a] = attr[a].at_double
            elif attr_str_ == "at_float":
                attrs[a] = attr[a].at_float
            elif attr_str_ == "at_int32":
                attrs[a] = attr[a].at_int32
            elif attr_str_ == "at_int64":
                attrs[a] = attr[a].at_int64
    return attrs


def fix_outputs(op_name, outputs):
    if op_name.lower() == "Dropout":
        if len(outputs) == 1:
            return outputs
        # TODO(zhreshold): support dropout mask?
        outputs = outputs[:-1]
    return outputs


def shape_of(x, dtype="int64"):
    ttype = infer_type(x).checked_type
    if not _ty.is_dynamic(ttype):
        shape = list(ttype.shape)
        return _expr.const(shape, dtype)
    return _op.shape_of(x, dtype)


def get_pad_pair(input1d, kernel1d, stride1d, mode):
    """infer pad size"""
    if input1d % stride1d == 0:
        pad = max(kernel1d - stride1d, 0)
    else:
        pad = max(kernel1d - (input1d % stride1d), 0)
    pad_before = pad // 2
    pad_after = pad - pad_before
    if "lower" in mode:
        return [pad_after, pad_before]
    return [pad_before, pad_after]


def autopad(
    data,
    strides,
    kernel_shape,
    dilations,
    ndim,
    pad_type="constant",
    deconv=False,
    mode="same_upper",
    pad_value=0.0,
):
    """
    Perform autopadding with dynamic input shapes
    """
    # get attributes as constants
    strides = _op.const(np.array(strides), dtype="int64")
    dilated_kernel_shape = _op.const(
        np.array(
            [(kernel - 1) * dilation + 1 for kernel, dilation in zip(kernel_shape, dilations)]
        ),
        dtype="int64",
    )
    # get input shape
    shape = _op.strided_slice(shape_of(data, dtype="int64"), [2], [ndim])

    # set up integer constants
    zero = _op.const(0, dtype="int64")
    one = _op.const(1, dtype="int64")
    two = _op.const(2, dtype="int64")

    # Calculate total padding
    mod = _op.mod(shape, strides)

    left = _op.maximum(dilated_kernel_shape - strides, zero)
    right = _op.maximum(dilated_kernel_shape - mod, zero)

    total_pad = _op.where(_op.equal(mod, zero), left, right)
    if deconv:
        total_pad = _op.const(np.array(kernel_shape), dtype="int64") - one - total_pad

    # split total padding into before and after
    pad_before = _op.floor_divide(total_pad, two)
    pad_after = total_pad - pad_before

    # combine
    if "lower" in mode:
        pad = _op.concatenate(
            [_op.reshape(pad_after, [-1, 1]), _op.reshape(pad_before, [-1, 1])], axis=1
        )
    else:
        pad = _op.concatenate(
            [_op.reshape(pad_before, [-1, 1]), _op.reshape(pad_after, [-1, 1])], axis=1
        )

    # pad N and C with zeros
    pad = _op.concatenate([_op.const(np.zeros([2, 2], dtype="int64"), dtype="int64"), pad], axis=0)

    if isinstance(pad_value, (float, int)):
        pad_value = _op.const(pad_value)

    return _op.nn.pad(data, fold_constant(pad), pad_value, pad_type)


def dimension_constraint():
    def _dim_check(attrs):
        if len(attrs["kernel_shape"]) in [1, 2, 3]:
            return True
        return False

    return _dim_check, "Only 1d, 2d and 3d kernel supported."


class OneFlowOpConverter:
    """A helper class for holding oneflow op converters."""

    @classmethod
    def get_converter(cls):
        """Get converter matches given opset.
        Parameters
        ----------
        
        Returns
        -------
        converter, which should be `_impl_vx`. Number x is the biggest
            number smaller than or equal to opset belongs to all support versions.
        """
        # TODO: version用来控制是用哪个函数
        version = 1
        if hasattr(cls, "_impl_v{}".format(version)):
            return getattr(cls, "_impl_v{}".format(version))
        raise NotImplementedError(
            "version {} of {} not implemented".format(version, cls.__name__)
        )


class Pool(OneFlowOpConverter):
    """A helper class for pool op converters."""

    name = ""

    @classmethod
    def _impl_v1(cls, inputs, attrs, params):
        data = inputs[0]
        input_shape = infer_shape(data)
        input_dtype = infer_type(data).checked_type.dtype
        ndim = len(input_shape)

        # TODO
        if attrs["data_format"] == "channels_first":
            attrs["data_format"] = "NCHW"
        else:
            attrs["data_format"] = "NHMC"

        if "padding" in attrs:
            if attrs["padding"].lower() in ("same_upper", "same_lower"):
                if cls.name == "avg_pool":
                    pad_tuple = []
                    for axis in range(len(input_shape) - 2):
                        axis_shape = input_shape[2 + axis]
                        stride = attrs.get("strides", [1] * ndim)[axis]
                        kernel = attrs["pool_size"][axis]
                        pad = get_pad_pair(axis_shape, kernel, stride, attrs["padding"])
                        pad_tuple.append(pad)
                    pad_tuple = tuple([val for pair in zip(*pad_tuple) for val in pair])
                    attrs["pads"] = pad_tuple
                else:
                    # Warning: Pool does not yet support dynamic shapes,
                    # one will need to run dynamic_to_static on this model after import
                    if "int" in input_dtype:
                        pad_val = np.iinfo(np.dtype(input_dtype)).min
                    else:
                        pad_val = np.finfo(np.dtype(input_dtype)).min
                    data = autopad(
                        data,
                        attrs.get("strides", [1] * (ndim - 2)),
                        attrs["pool_size"],
                        [1] * ndim,
                        ndim,
                        pad_value=pad_val,
                        mode=attrs["padding"].upper(),
                    )
            
            elif attrs["padding"].lower() == "valid":
                attrs["pads"] = tuple([0 for _ in range(ndim - 2)])
            elif attrs["padding"].lower() == "same":
                # TODO
                pass
        
            else:
                msg = 'Value {} in attribute "padding" of operator {} is invalid.'
                raise tvm.error.OpAttributeInvalid(msg.format(attrs["padding"], cls.name))
            attrs.pop("padding")

        if "padding_before" not in attrs:
            attrs["padding_before"] = [0, 0]
        if "padding_after" not in attrs:
            attrs["padding_after"] = [0, 0]
            attrs["pads"] = [attrs["padding_after"][0], attrs["padding_before"][0],
                             attrs["padding_after"][1], attrs["padding_before"][1]]
        attrs.pop("padding_before")
        attrs.pop("padding_after")

        out = AttrCvt(
            op_name=cls.name,
            transforms={
                "kernel_shape": "pool_size",
                "pads": ("padding", 0),
                "dilations": ("dilation", 1),
            },
            # TODO: NCHW与其他数据之间的转换
            ignores=["storage_order", "data_format"],
            # TODO: ??? transforms doesn't work ???
            # custom_check=dimension_constraint(),
        )([data], attrs, params)

        return out


class Conv(OneFlowOpConverter):
    """Operator converter for Conv."""
    name = ""

    @classmethod
    def _impl_v1(cls, inputs, attrs, params):
        # Use shape of input to determine convolution type.
        data = inputs[0]
        kernel = inputs[1]
        input_shape = infer_shape(data)
        ndim = len(input_shape)

        kernel_type = infer_type(inputs[1])
        kernel_shapes = [get_const_tuple(kernel_type.checked_type.shape)]

        if "kernel_size" not in attrs:
            attrs["kernel_size"] = kernel_shapes[0][2:]

        if "padding" in attrs:
            if attrs["padding"].lower() in ("same_upper", "same_lower"):
                # Warning: Convolution does not yet support dynamic shapes,
                # one will need to run dynamic_to_static on this model after import
                data = autopad(
                    data,
                    attrs.get("strides", [1] * (ndim - 2)),
                    attrs["kernel_size"],
                    attrs.get("dilation_rate", [1] * (ndim - 2)),
                    ndim,
                    mode=attrs["padding"].upper(),
                )
            elif attrs["padding"].lower() == "vaild":
                attrs["pads"] = [0 for i in range(ndim - 2)]
            elif attrs["padding"].lower() == "same":
                pass
            else:
                msg = 'Value {} in attribute "padding" of operator Conv is invalid.'
                raise tvm.error.OpAttributeInvalid(msg.format(attrs["padding"]))
            attrs.pop("padding")

        if "dilation_rate" in attrs:
            # TODO: transforms doesn't work
            attrs["dilations"] = list(attrs["dilation_rate"])
            attrs.pop("dilation_rate")

        group_conv1d = False
        if cls.name == "conv1d" and attrs.get("groups") != 1:
            group_conv1d = True
            # Expand input from NCW to NCHW
            data = _op.expand_dims(data, axis=2)
            # Expand kernel from OIW to OIHW
            kernel = _op.expand_dims(kernel, axis=2)
            # Add new value to kernel_shape, strices, dilation, pads, if needed
            attrs["kernel_size"] = [1] + list(attrs["kernel_size"])
            if "strides" in attrs:
                attrs["strides"] = [1] + list(attrs["strides"])
            if "dilation_rate" in attrs:
                attrs["dilations"] = [1] + list(attrs["dilation_rate"])
                attrs.pop("dilation_rate")

        if "padding_before" not in attrs:
            attrs["padding_before"] = [0, 0]
        if "padding_after" not in attrs:
            attrs["padding_after"] = [0, 0]
        attrs["pads"] = [attrs["padding_after"][0], attrs["padding_before"][0],
                         attrs["padding_after"][1], attrs["padding_before"][1]]
        attrs.pop("padding_before")
        attrs.pop("padding_after")

        out = AttrCvt(
            op_name=cls.name,
            transforms={
                "kernel_shape": "kernel_size",
                "pads": ("padding", 0),
                "group": ("groups", 1),
            },
            # TODO: conv2d() got an unexpected keyword argument 'dilations' ???
            ignores=["data_format", "filters", "dilations"],
            # custom_check=dimension_constraint(),
        )([data, kernel], attrs, params)

        # If this was a group_conv1d, squish output back to NCW.
        if group_conv1d:
            out = _op.squeeze(out, axis=[2])

        # oneflow里面bias-add是一个专门的一个op, 用Add替换
        return out


class Conv2d(Conv):
    """Operator converter for Conv2d."""
    name = "conv2d"


class BatchNorm(OneFlowOpConverter):
    """Operator converter for BatchNorm."""

    @classmethod
    def _impl_v1(cls, inputs, attrs, params):
        out = AttrCvt(
            op_name="batch_norm", ignores=["spatial", "is_test", "consumed_inputs", "momentum"]
        )(inputs, attrs, params)
        return out[0]


class InstanceNorm(OneFlowOpConverter):
    """Operator converter for InstanceNorm."""

    @classmethod
    def _impl_v1(cls, inputs, attrs, params):
        return AttrCvt(op_name="instance_norm")(inputs, attrs, params)


class MatMul(OneFlowOpConverter):
    """Operator converter for MatMul."""
    # TODO: 这个应该对应的是onnx.py中的GEMM

    @classmethod
    def _impl_v1(cls, inputs, attrs, params):
        assert len(inputs) == 3 or len(inputs) == 2, "Gemm op take 2 or 3 inputs, {} given".format(
            len(inputs)
        )
        dtype = infer_type(inputs[0]).checked_type.dtype
        # Y = alpha * A * B + beta * C
        alpha = float(attrs.get("alpha", 1.0))
        beta = float(attrs.get("beta", 1.0))
        transA = bool(attrs.get("transpose_a", False))
        transB = bool(attrs.get("transpose_b", False))
        # get number of channels
        channels = infer_channels(inputs[1], not transB)
        if transA:
            inputs[0] = _op.transpose(inputs[0], axes=(1, 0))
        if not transB:
            inputs[1] = _op.transpose(inputs[1], axes=(1, 0))
        inputs[0] = _op.nn.batch_flatten(inputs[0])
        if alpha != 1.0:
            inputs[0] *= _expr.const(alpha, dtype=dtype)
        out = _op.nn.dense(inputs[0], inputs[1], units=channels)
        if len(inputs) == 3:
            out = out + _expr.const(beta, dtype=dtype) * inputs[2]

        return out


class Elemwise(OneFlowOpConverter):
    """A helper class for elemwise op converters."""

    name = ""

    @classmethod
    def _impl_v1(cls, inputs, attrs, params):
        assert len(inputs) == 2, "Math op {} take 2 inputs, {} given".format(cls.name, len(inputs))
        op_name = cls.name
        conv_ops = ["conv2d", "conv2d_transpose"]
        if attrs.get("broadcast", 0) and any(x in str(inputs[0]) for x in conv_ops):
            # TODO(zhreshold): remove hard coded infershape
            axis = int(attrs.get("axis", 0))
            inputs[1] = _op.expand_dims(inputs[1], axis=axis, num_newaxis=2)
        return get_relay_op(op_name)(*inputs)


class Add(Elemwise):
    """Operator converter for Add."""

    name = "add"


class MaxPool(Pool):
    """Operator converter for MaxPool"""

    name = "max_pool2d"


class AveragePool(Pool):
    """Operator converter for AveragePool."""

    name = "avg_pool"


class Reshape(OneFlowOpConverter):
    """Operator converter for Reshape."""

    @classmethod
    def _impl_v1(cls, inputs, attrs, params):
        out = _op.reshape(inputs[0], attrs["shape"])

        return out

    
class Softmax(OneFlowOpConverter):
    """Operator converter for Softmax."""

    @classmethod
    def _impl_v1(cls, inputs, attr, params):
        axis = attr.get("axis", 1)
        ndim = len(infer_shape(inputs[0]))
        if axis < 0:
            axis += ndim
        axes = list(range(axis, ndim))
        x = inputs[0]
        m = _op.max(x, axes, keepdims=True)
        e = _op.exp(x - m)
        return e / _op.sum(e, axes, keepdims=True)


class LogSoftmax(OneFlowOpConverter):
    """Operator converter for Softmax."""

    @classmethod
    def _impl_v1(cls, inputs, attr, params):
        axis = attr.get("axis", 1)
        ndim = len(infer_shape(inputs[0]))
        if axis < 0:
            axis += ndim
        axes = list(range(axis, ndim))
        x = inputs[0]
        m = _op.max(x, axes, keepdims=True)
        e = _op.exp(x - m)
        s = _op.sum(e, axes, keepdims=True)
        return x - m - _op.log(s)


class Dropout(OneFlowOpConverter):

    @classmethod
    def _impl_v1(cls, inputs, attr, params):
        out = AttrCvt("dropout", {"ratio": "rate"}, ignores=["is_test"])

        return out


def get_convert_map():
    # TODO: 记录实现的oneflow2relay op
    return {
        # defs/math
        "bias_add": Add.get_converter(), # TODO: 这个oneflow多了一个axis，可能需要修改
        "log": Renamer("log"),
        "acos": Renamer("acos"),
        "acosh": Renamer("acosh"),
        "asin": Renamer("asin"),
        "asinh": Renamer("asinh"),
        "atan": Renamer("atan"),
        "atanh": Renamer("atanh"),
        "cos": Renamer("cos"),
        "cosh": Renamer("cosh"),
        "sin": Renamer("sin"),
        "sinh": Renamer("sinh"),
        "tan": Renamer("tan"),
        "tanh": Renamer("tanh"),
        "pow": Renamer("power"),
        "exp": Renamer("exp"),
        "floor": Renamer("floor"),
        "ceil": Renamer("ceil"),
        "round": Renamer("round"),
        # defs/activation
        "sigmoid": Renamer("sigmoid"),
        "relu": Renamer("relu"),
        # defs/nn
        "conv2d": Conv2d.get_converter(),
        "max_pool_2d": MaxPool.get_converter(),
        "dropout": Dropout.get_converter(),
        # defs/tensor
        "matmul": MatMul.get_converter(), # TODO: 究竟是matmul还是gemm
        # defs/others
        "reshape": Reshape.get_converter(), # onnx.py中这个跟resize还不太一样
    }


class Softplus(OneFlowOpConverter):
    """Operator converter for Softplus."""

    @classmethod
    def _impl_v1(cls, inputs, attr, params):
        data = inputs[0]
        data_dtype = infer_type(data).checked_type.dtype
        data = _op.exp(data) + _expr.const(1, dtype=data_dtype)
        return _op.log(data)


class oneflow_input(object):
    """
    Dual purpose list or dictionary access object
    copy from ./onnx.py
    """
    def __init__(self):
        self.input_keys = []
        self.input_dict = {}

    def __getitem__(self, item):
        if isinstance(item, int):
            if item > (len(self.input_keys) - 1):
                return None
            return self.input_dict[self.input_keys[item]]
        if isinstance(item, str):
            if item not in self.input_keys:
                return None
            return self.input_dict[item]
        if isinstance(item, slice):
            keys = self.input_keys[item]
            return [self.input_dict[key] for key in keys]

        raise ValueError("Only integer, string, and slice accesses allowed.")

    def __setitem__(self, item, value):
        if isinstance(item, int):
            self.input_dict[self.input_keys[item]] = value
        elif isinstance(item, str):
            self.input_keys.append(item)
            self.input_dict[item] = value
        else:
            raise ValueError("Only integer and string indexed writes allowed.")

    def keys(self):
        return self.input_keys

    def __len__(self):
        return len(self.input_keys)

    def __iter__(self):
        self.n = 0
        return self

    def __next__(self):
        if self.n < len(self.input_keys):
            output = self.input_dict[self.input_keys[self.n]]
            self.n += 1
            return output

        raise StopIteration


class OneflowGraph(object):
    """
    A helper class for handling Relay expression

    Parameters
    ----------
    shape : dict of str to tuple, optional
        The input shape to the graph
    dtype : dict of str to str
        The input types to the graph
    """
    def __init__(self, shape, dtype, nodes, model_dir_path) -> None:
        self._nodes = {}
        self._params = {}
        self._renames = {}
        self._inputs = {}
        self._num_input = 0
        self._num_param = 0
        self._input_names = []
        self._input_name_2_path = {}
        self._model_array = {}
        self._outputs = []
        self._shape = shape
        self._dtype = dtype

        import oneflow

        model = oneflow.checkpoint.get(model_dir_path)
        # model_array是以layer_name为key，以dict('path', 'params')为value的dict
        for layer in model:
            layer_p = {}
            layer_p['path'] = model[layer].file_path # 模型参数所在路径
            layer_p['params'] = model[layer].numpy() # 模型各层ndarray
            self._model_array[str(layer)] = layer_p

        for node_name in nodes:
            node = nodes[node_name]
            if is_user_op(node):
                for input_name in node.user_conf.input:
                    node_init_name = node_name + '-' + input_name
                    node_input_path = getattr(node.user_conf.input[input_name], 's')
                    if len(node_input_path) == 1:
                        node_input_path = os.path.join(model_dir_path, node_input_path[0])
                    else:
                        pass

                    self._input_name_2_path[node_init_name] = node_input_path

                    for node_input_name in self._model_array:
                        node_p = self._model_array[node_input_name]

                        if node_input_path == node_p['path']:
                            node_array = node_p['params']
                            self._params[node_init_name] = node_array
                            self._nodes[node_init_name] = new_var(
                                node_init_name, 
                                shape=node_array.shape,
                                dtype=str(node_array.dtype)
                            )
        """
        node_outputs的名字不会直接出现在node.user_conf.input里面
        所以在构建计算图的时候会导致层与层之间的联系被斩断
        修补步骤：
        1. 找到此时node_outputs对应的路径
        2. 将该路径与node.user_conf.input里面的对应起来，也就是说，需要在__init__的时候创建一个
            所有node.user_conf.input与其路径的dict
        3. dict反转，找到与node_outputs同一路径的node_input(下句)
        4. 在output的new_var的时候将node_outputs的名字换成找到的node_input的名字
        """
        self._input_path_2_name = {v: k for k, v in self._input_name_2_path.items()}

        self._output_path = {}
        for node_name in nodes:
            node = nodes[node_name]
            if is_output_op(node):
                output_path = os.path.join(model_dir_path, getattr(node.return_conf, "in"))
                self._output_path[node.name] = output_path

        # print("output path: -------------")
        # for n in self._output_path:
        #     print(n, self._output_path[n])
        #     print()

        # print("known param: -------------")
        # for n in self._params:
        #     print(n)
        #     print()

        # print("known shape: -------------")
        # for n in self._shape:
        #     print(n, self._shape[n])
        #     print()


    def _parse_input(self, node, model_dir_path):
        for input_name in node.user_conf.input:
            node_input_name = node.name + '-' + input_name

            node_input_path = getattr(node.user_conf.input[input_name], 's')
            if len(node_input_path) == 1:
                node_input_path = os.path.join(model_dir_path, node_input_path[0])
            else:
                pass

            node_input_shape = self._shape[node_input_path]
            node_input_dtype = self._dtype[node_input_path]

            if node_input_name != "":
                if node_input_name not in self._nodes:
                    self._nodes[node_input_name] = new_var(
                        node_input_name,
                        shape=node_input_shape,
                        dtype=node_input_dtype
                    )


    def from_oneflow(self, nodes, model_dir_path):
        """
        Parameters
        ----------
        nodes : dict, keys: node.name, value: node
            contain the graph
        model_dir_path: str
            The path of parameter

        Returns
        -------
        mod : tvm.IRModule
            The returned relay module
        params : dict
            A dict of name: tvm.nd.array pairs, used as pretrained weights
        """
        # step 1: get the graph input
        for node_name in nodes:
            node = nodes[node_name]
            if is_input_op(node):
                if node_name not in self._nodes:
                    self._nodes[node_name] = new_var(
                        node_name, 
                        shape=tuple(node.input_conf.blob_conf.shape.dim),
                        dtype=NP_2_TVM_DTYPE[FLOW_2_NP_DTYPE[node.input_conf.blob_conf.data_type]]
                    )
                self._inputs[node_name] = self._nodes[node_name]

        # step 2: find out if unsupported ops are used
        # 获取中间计算过程的oneflow2relay op，为后面转换中间计算过程的op做准备
        convert_map = get_convert_map()
        unsupported_ops = set()
        for node_name in nodes:
            node = nodes[node_name]
            # 开始转换中间计算过程的op(user_op)
            if is_user_op(node):
                # 这里应该是op的type，而不是神经网络中用户指定的层的名字
                op_name = node.user_conf.op_type_name
                if(
                    # TODO: 这个if语句需要根据op转换的具体工作做修正
                    op_name not in convert_map
                    and op_name not in _identity_list
                ):
                    unsupported_ops.add(op_name)
        # 如果遇到不能转换的op，报错
        if unsupported_ops:
            msg = "The following operators are not supported for frontend OneFlow: "
            msg += ", ".join(unsupported_ops)
            raise tvm.error.OpNotImplemented(msg)

        # step 3: convert op
        print("converting: ----------")
        for node_name in nodes:
            node = nodes[node_name]
            if is_user_op(node):
                op_name = node.user_conf.op_type_name
                op_attr = parse_attr(node.user_conf.attr)

                self._parse_input(
                    node,
                    model_dir_path=model_dir_path
                )

                node_inputs = oneflow_input()
                for input_name in node.user_conf.input:
                    node_input_name = node_name + '-' + input_name
                    if node_input_name != "":
                        o = self._renames.get(node_input_name, node_input_name)
                        node_inputs[node_input_name] = self._nodes[o]
                    else:
                        node_inputs[node_input_name] = None

                node_outputs = []
                for output_name in node.user_conf.output:
                    node_output_name = str(node_name) + '-' + str(output_name)

                    node_output_path = getattr(node.user_conf.output[output_name], 's')
                    if len(node_output_path) == 1:
                        node_output_path = os.path.join(model_dir_path, node_output_path[0])
                    else:
                        pass
                    
                    if node_output_path in self._input_path_2_name:
                        node_outputs.append(self._input_path_2_name[node_output_path])
                    else:
                        warnings.warn("{} is not in input_path".format(node_output_path))

                node_outputs = fix_outputs(op_name, node_outputs)
                # TODO: night（fix ops)

                print("node output: ---------")
                print(node_outputs)
                print()

                op_attr["tvm_custom"] = {}
                # onnx.py这里实际的name是空，没有看明白
                # 原因: .common.py line 405: ignore 'tvm_custom' always
                # TODO: 还没有实验删掉这句话会有什么影响
                op_attr["tvm_custom"]["name"] = ''
                op_attr["tvm_custom"]["num_outputs"] = len(node_outputs)

                # 转换核心语句
                op = self._convert_operator(op_name, node_inputs, op_attr)

                # 判断网络有多少个输出，并相应做出调整
                if not isinstance(op, _expr.TupleWrapper):
                    outputs_num = 1
                else:
                    outputs_num = len(op)

                assert (len(node_outputs) == outputs_num), "Number of output mismatch {} vs {} in {}.".format(
                    len(node_outputs), outputs_num, op_name
                )

                if outputs_num == 1:
                    op = fold_constant(op)
                else:
                    op = _expr.TupleWrapper(fold_constant(op.astuple()), len(op))

                # TODO: 关于可选输出与输出的清洗，oneflow可能暂时不需要
                if outputs_num > 1:
                    pass

                if outputs_num == 1:
                    self._nodes[node_outputs[0]] = op
                    self._outputs.append(node_outputs[0])
                else:
                    for k, i in zip(list(node_outputs), range(len(node_outputs))):
                        self._outputs.append(k)
                        self._nodes[k] = op[i]
                print()

        print("convert ends.")
        outputs = []
        for node_name in nodes:
            node = nodes[node_name]
            if is_user_op(node):
                for output_name in node.user_conf.output:
                    node_output_name = str(node_name) + '-' + str(output_name)

                    node_output_path = getattr(node.user_conf.output[output_name], 's')
                    if len(node_output_path) == 1:
                        node_output_path = os.path.join(model_dir_path, node_output_path[0])
                    else:
                        pass

                    if node_output_name in self._outputs or node_output_path in self._output_path:
                        outputs.append(self._nodes[node_output_name])

        outputs = outputs[0] if len(outputs) == 1 else _expr.Tuple(outputs)

        # 转换为relay IR
        free_vars = analysis.free_vars(outputs)

        nodes = {v: k for k, v in self._nodes.items()}
        free_vars = [nodes[var] for var in free_vars]

        # free_vars都应该存储到self._inputs里头
        for free_var in free_vars:
            if free_var not in self._inputs:
                self._inputs[free_var] = self._nodes[free_var]

        # Create a function from our output expression and all input variables.
        func = _function.Function([v for _, v in self._inputs.items()], outputs)
        print(func)

        return IRModule.from_expr(func), self._params


    def _convert_operator(self, op_name, node_inputs, op_attr):
        """
        Parameters
        ----------
        op_name : str
            Operator name, such as conv2d、relu
        node_inputs : list of tvm.relay.function.Function
            List of inputs.
        op_attr : dict
            Dict of operator attributes

        Returns
        -------
        sym : tvm.relay.function.Function
            Converted relay function
        """
        convert_map = get_convert_map()
        if op_name in _identity_list:
            sym = get_relay_op(op_name)(*node_inputs, **op_attr)
        elif op_name in convert_map:
            # conver_map[op_name]用来获取是哪一个op
            # node_inputs: oneflow_input类
            # op_attr
            sym = convert_map[op_name](node_inputs, op_attr, self._params)
        else:
            raise NotImplementedError("Operator {} not implemented.".format(op_name))

        return sym


def from_oneflow(eval_job, model_dir_path, shape=None, dtype=None):
    """
    Parameters
    ----------
    eval_job : job function, type='predict'
    model_dir_path: str
        The path of parameter
    shape : dict of str to tuple, optional
        The input shape to the graph, keys: node_param_path, values: shape
    dtype : str or dict of str to str
        The input types to the graph, keys: node_param_path, values: dtype

    Returns
    -------
    mod : tvm.IRModule
        The relay module for compilation
    params : dict of str to tvm.nd.NDArray
        The parameter dict to be used by relay
    """
    try:
        import oneflow
        import oneflow.experimental as flow

        # flow.enable_eager_execution()
        oneflow.config.enable_legacy_model_io(False)

        # 判断模型参数是否可以正常导入
        if 'snapshot_done' not in os.listdir(model_dir_path):
            raise IndexError("\'snapshot_name\' is not in the model path, \
            please determine whether the model has been trained")

    except ImportError:
        raise ImportError("please check that OneFlow is installed")

    # 获取job函数的所有可能信息，用于得到用户的job，导出计算图
    job_set = flow.get_job_set()

    # 创建一个以node.name为key，以node为value的字典，避免后续大量for循环查找浪费时间
    nodes = {}
    shape = {}
    dtype = {}
    for job in job_set.job:
        if job.job_conf.job_name == eval_job.__name__:
            for node in job.net.op:
                nodes[node.name] = node
            # 不需要跑出来中间变量，这里都存储好了
            for lbn in job.helper.lbn2logical_blob_desc:
                lbd = job.helper.lbn2logical_blob_desc[lbn]
                node_path = os.path.join(model_dir_path, lbn)
                node_shape = tuple(lbd.shape.dim)
                node_dtype = lbd.data_type
                shape[node_path] = node_shape
                dtype[node_path] = NP_2_TVM_DTYPE[FLOW_2_NP_DTYPE[node_dtype]]

    g = OneflowGraph(shape, dtype, nodes, model_dir_path)

    # Use the graph proto as a scope so that ops can access other nodes if needed.
    mod, params = g.from_oneflow(nodes=nodes, model_dir_path=model_dir_path)
    return mod, params
