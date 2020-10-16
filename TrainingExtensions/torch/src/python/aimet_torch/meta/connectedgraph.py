#!/usr/bin/env python3.5

#  =============================================================================
#
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2019-2020, Qualcomm Innovation Center, Inc. All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice,
#     this list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
#  3. Neither the name of the copyright holder nor the names of its contributors
#     may be used to endorse or promote products derived from this software
#     without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
#  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.
#
#  SPDX-License-Identifier: BSD-3-Clause
#
#  @@-COPYRIGHT-END-@@
#
#  =============================================================================

"""For constructing a uniform representation of the computational graph for a PyTorch model,
that is easy to navigate and stores information for the purpose of winnowing.
The representation graph consists of nodes that are either 'operation' or 'product';
operations represent a module or a function that generates a tensor, while products represent
the tensors that are either input to the model (input, constant or parameter) or the
result of an operation. Furthermore the graph representation is bi-directional."""

import re
from typing import Tuple, Union, List, Dict
import torch

from aimet_common.connected_graph.connectedgraph import ConnectedGraph as AimetCommonConnectedGraph, get_ordered_ops
from aimet_common.connected_graph.product import Product
from aimet_common.connected_graph.operation import Op, determine_preceding_op_input_product_index_in_multi_input_op
from aimet_common.model_module import PytorchModelModule
from aimet_common.utils import AimetLogger, ModelApi, api_channel_index_dict
from aimet_torch.utils import is_leaf_module, run_hook_for_layers_with_given_input
from aimet_torch.defs import PassThroughOp

logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.Winnow)


class ConnectedGraph(AimetCommonConnectedGraph):
    """
    For construction of a graph that connects operations together (
        either module or functional) as producers and consumers of tensors.
        Note that the graph has two kinds of nodes: operations and products."""

    def __init__(self, model: torch.nn.Module, model_input: Tuple[torch.Tensor]):
        """
        Init function for connected graph
        :param model: Pytorch model to create connected graph from
        :param model_input: Example input to model.  Can be a single tensor or a list/tuple of input tensors
        """
        super().__init__()
        self._model_name = type(model).__name__
        # Maps pytorch module names to modules
        self._name_to_module = {}
        # Maps pytorch modules to module names
        self._module_to_name = {}
        # Maps pytorch modules to connected graph ops
        self._module_to_op_dict = {}

        # Parameters dict to hold parameters identified from trace code, to be made into Products
        # Maps parameter name as found in trace code to tuple of (corresponding module, parameter type, parameter shape)
        self._parameters = {}

        # Ops dict to map names of ops as found in trace code to corresponding Ops that are created
        self._named_ops = {}

        self._op_count = 0
        self._split_count = 0  # Use it in the name of split Ops getting added to the connected graph.

        # List of ops in the order they are traversed using the forward function
        self.ordered_ops = []

        self._generate_module_lookup_table(model)
        self._construct_graph(model, model_input)
        self._validate_op_modules()

    # Map torch module types to normalized names to provide backward compatibility to
    # trace code based construction
    op_type_map = {
        torch.nn.Conv2d: 'convolution',
        torch.nn.ConvTranspose2d: 'convolution',
        torch.nn.BatchNorm1d: 'batch_norm',
        torch.nn.BatchNorm2d: 'batch_norm',
        torch.nn.ReLU: 'relu',
        torch.nn.ReLU6: 'hardtanh',
        torch.nn.MaxPool2d: 'max_pool2d',
        torch.nn.AdaptiveAvgPool2d: 'adaptive_avg_pool2d',
        torch.nn.AvgPool2d: 'avg_pool2d',
        torch.nn.Linear: 'addmm',
        torch.nn.Dropout: 'dropout',
        torch.nn.Dropout2d: 'feature_dropout',
        torch.nn.LogSoftmax: 'log_softmax',
        torch.nn.Sigmoid: 'sigmoid'
    }

    functional_ops = {
        'cat',
        'size',
        'NumToTensor',
        'view',
        'add',
        'sub',
        'mul',
        'div',
        'narrow',
        'reshape',
        'mean',
        'index_select',
        'slice',
        'select',
        'unsqueeze',
        'Split'
    }

    # Graph nodes for which which we will completely ignore and skip processing
    ignore_graph_nodes = [
        "prim::Constant",
        "prim::ListConstruct",
        "aten::Int",
        "aten::t",
        "aten::to",
        "aten::detach"
    ]

    def __del__(self):
        """
        Destructor of ConnectedGraph class
        break the dependencies of Ops with Product
        """
        for product in self._products.values():
            product.producer = None
            product.set_consumers_to_null()

    def get_op_from_module_name(self, name: str) -> Union[Op, None]:
        """
        Given the name of a operation/module, return the corresponding op in ops dict
        :param name: Pytorch module name
        :return: Connected graph operation corresponding to pytorch module name.  Returns None if not found
        """
        module = self._name_to_module.get(name, None)
        if module:
            return self._module_to_op_dict.get(module, None)
        return None

    def get_all_ops(self) -> Dict[str, Op]:
        """ Returns the ops dictionary """
        return self._ops

    def get_all_products(self) -> Dict[str, Product]:
        """ Returns the products dictionary """
        return self._products

    def get_product(self, name: str) -> Product:
        """
        Returns the product with the name passed in the argument
        :param name: Product name
        """
        return self._products.get(name, None)

    def _generate_module_lookup_table(self, model: torch.nn.Module):
        """
        Generates a look up dictionary for getting modules from their names.
        :param model: Pytorch model
        """
        for name, module in model.named_modules(prefix=self._model_name):
            self._name_to_module[name] = module
            self._module_to_name[module] = name

    @staticmethod
    def _generate_module_tensors_lookup_table(model: torch.nn.Module, model_input: Tuple[torch.Tensor]) -> \
            Dict[str, Tuple[Tuple[torch.Tensor]]]:
        """
        Generates a look up dictionary for getting modules from their names.
        :param model: Pytorch model
        :return: Map of modules and input and output tensor obtained from a forward pass
        """
        module_tensor_tuples_map = {}

        def forward_hook(curr_module: torch.nn.Module,
                         input_tensor_tuple: Tuple[torch.Tensor],
                         output_tensor_tuple: Tuple[torch.Tensor]):
            """
            Custom forward hook function to add every module to module-to-tensor dict.
            :param curr_module: Current module being traversed during forward pass.
            :param input_tensor_tuple: tuple of input tensors to the current module
            :param output_tensor_tuple: tuple of output tensors of the current module
            """
            # Currently, we assume that multiple input tensors have the same shape, and likewise for output tensors.
            module_tensor_tuples_map[curr_module] = (input_tensor_tuple, output_tensor_tuple)

        run_hook_for_layers_with_given_input(model, model_input, forward_hook, leaf_node_only=False)

        return module_tensor_tuples_map

    def _construct_graph(self, model: torch.nn.Module, model_input: Tuple[torch.Tensor]):
        """
        Construct connected graph from model and example inputs.
        :param model: Pytorch model to create connected graph from
        :param model_input: Example input to model.  Can be a single tensor or a list/tuple of input tensors
        """
        module_tensor_tuples_map = ConnectedGraph._generate_module_tensors_lookup_table(model, model_input)
        trace = torch.jit.trace(model, model_input)
        _ = self._parse_trace_graph(trace, model, model_input, module_tensor_tuples_map)
        self._remove_tuple_ops()
        # Create parameters for ops such as conv, batchnorm, etc.
        self._fill_op_params()

        # For each split in the model, insert a corresponding split Op in the connected graph.
        ops_list = [op for op in self._ops.values()]
        for op in ops_list:
            self._determine_split_behavior_for_op_and_insert_split_op_in_connected_graph(op)

        self._fill_empty_shapes()

    def _parse_trace_graph(self, trace: Union[torch.jit.TopLevelTracedModule, torch.jit.TracedModule],
                           model: torch.nn.Module, model_input: Union[Tuple[torch.Tensor], List[Union[Op, Product]]],
                           module_tensor_tuples_map: Dict[str, Tuple[Tuple[torch.Tensor]]]) -> Op:
        # pylint: disable=protected-access
        """
        Implements a depth-first graph extraction to create an equivalent connected graph representation
        with Ops and Products. depth-first extraction is realized using recursion.

        :param trace: Pytorch JIT trace for model or a submodule
        :param model: Pytorch model to create connected graph from
        :param model_input: Example input to model.  Can be a single tensor or a list/tuple of input tensors
        :param module_tensor_tuples_map: Map of modules and input and output tensor obtained from a forward pass
        :return: the last created Op in the model or submodule
        """

        # graph node equivalent Ops or Products locally constructed or populated via recursive invocation
        # initialized with input products
        model_debug_name, ops = self._create_input_products(model_input, trace.graph)

        if is_leaf_module(model):
            return self._parse_single_module_model(model, module_tensor_tuples_map, ops, trace.graph)

        # A map of sub-graph models and node name that requires recursive parsing
        node_name_to_subgraph_model = {}
        # modules that are being referenced within the sub-graph
        node_name_to_module = {model_debug_name: model}
        for node in trace.graph.nodes():

            if 'prim::TupleUnpack' in node.kind():
                # 'TupleUnpack' node generates multiple 'named' output tensor which is referenced in the current
                # sub-graph and is currently the only known way a multi-output op manifests in PyTorch model.
                #
                #  A TupleUnpack node has the following construct:
                #       %X : nn.Module = prim::GetAttr[...]
                #       %Y : (Tensor, Tensor) = prim::CallMethod[name="forward"](%X, ...)
                #       %Z.1 : Tensor, %Z.2 : Tensor = prim::TupleUnpack(%Y)
                #
                # the current workaround is to introduce a TupleUnpack 'functional' op per output tensor
                # i.e. %Z.1 and %Z.2 as consumer for the module referenced in %Y( & %Z) above.
                # when support is added for multiple output Op and Product tracking of consumer and producer w/ indexed
                # output tensor then %Z.* will be subsumed into handling of %Y.
                self._create_tuple_ops(node, module_tensor_tuples_map, node_name_to_module, ops)
            else:
                if node.outputsSize() != 1:
                    logger.error("multiple output Ops are not supported %s", str(node))
                    raise NotImplementedError

                output_name: str = node.output().debugName()

                # retrieving a module reference
                if 'GetAttr' in node.kind():
                    subgraph_model = ConnectedGraph._get_module_instance(node, node_name_to_module)
                    if output_name not in node_name_to_module:
                        node_name_to_module[output_name] = subgraph_model
                    else:
                        raise ValueError("duplicate model for {0} -> {1} and {2}".format(
                            output_name, node_name_to_module[output_name], subgraph_model))
                    if not is_leaf_module(subgraph_model):
                        node_name_to_subgraph_model[output_name] = subgraph_model

                # invoking forward method
                elif 'CallMethod' in node.kind():
                    self.parse_callmethod_node(node, model, trace, module_tensor_tuples_map,
                                               node_name_to_module, ops, node_name_to_subgraph_model)

                # functional operations e.g. cat, size etc
                elif node.kind() not in self.ignore_graph_nodes:
                    ops[output_name] = self._create_functional_op(node, ops)

        # return the last op enqueued in the sub-graph forward pass
        return self.ordered_ops[-1]

    def _create_tuple_ops(self, node: torch._C.Node,
                          module_tensor_tuples_map: Dict[str, Tuple[Tuple[torch.Tensor]]],
                          node_name_to_module: Dict[str, torch.nn.Module],
                          ops: Dict[str, Union[Op, Product]]):
        # pylint: disable=protected-access
        """
        parses a 'TupleUnpack' node and generates multiple TupleUnpack functional ops to account for each output tensor
        :param node: trace graph node representing the i.e. 'TupleUnpack' node
        :param module_tensor_tuples_map: Map of modules and input and output tensor obtained from a forward pass
        :param node_name_to_module: dictionary of module indexed by output_name referenced in the sub-graph
        :param ops: dictionary of Ops and Products indexed by output names referenced in the graph
        """

        # Traverse the graph back to the module get instance
        node_name = next(node.input().node().inputs()).debugName()
        _, output_tensor_tuple = module_tensor_tuples_map[node_name_to_module[node_name]]

        # flatten the output tensors which may contain tuple of tuple to a list
        output_tensors = []
        for output_tensor in output_tensor_tuple:
            if isinstance(output_tensor, torch.Tensor):
                output_tensors.append(output_tensor)
            else:
                output_tensors.extend(list(output_tensor))

        for i, output in enumerate(node.outputs()):
            inputs = self._resolve_input_nodes(node)

            op = self._create_op_and_products(ConnectedGraph._parse_op_type(node), inputs, ops)

            # first input tensor is assumed to define the shape of the input tensor(s)
            inp_op = ops[inputs[0].debugName()]
            _fill_and_check_op_product_shapes(op,
                                              inp_op.shape if isinstance(inp_op, Product) else inp_op.output_shape,
                                              list(output_tensors[i].shape))
            ops[output.debugName()] = op

    @staticmethod
    def _parse_op_type(node: torch._C.Node) -> str:
        # pylint: disable=protected-access
        """
        Helper method to extract op type from node info
        :param node: trace graph node
        :return: Op Type string
        """
        # extracting Op type from node.kind string e.g. aten::relu_, aten::size etc
        op_type = node.kind().split("::")[-1].lstrip('_').rstrip('_')
        return op_type

    def _parse_single_module_model(self, module: torch.nn.Module,
                                   module_tensor_tuples_map: Dict[str, Tuple[Tuple[torch.Tensor]]],
                                   ops: Dict[str, Union[Op, Product]],
                                   graph: torch._C.Graph) -> Op:
        # pylint: disable=protected-access
        """
        Creates a fully populated Op and along with associated products representing inputs for the model
        :param module:  Pytorch model composed on single module
        :param module_tensor_tuples_map: Map of modules and input and output tensor obtained from a forward pass
        :param ops: dictionary of Ops and Products indexed by output names referenced in the graph
        :param graph: trace graph representing the model
        :return: Ops
        """
        inputs = []
        for inp in graph.inputs():
            inputs.append(inp)
        return self._create_leaf_module_op(module, inputs, ops, module_tensor_tuples_map)

    def parse_callmethod_node(self, node: torch._C.Node,
                              model: torch.nn.Module,
                              trace: Union[torch.jit.TopLevelTracedModule, torch.jit.TracedModule],
                              module_tensor_tuples_map: Dict[str, Tuple[Tuple[torch.Tensor]]],
                              node_name_to_module: Dict[str, torch.nn.Module],
                              ops: Dict[str, Union[Op, Product]],
                              node_name_to_subgraph_model: Dict[str, torch._C.Node]):
        # pylint: disable=protected-access
        # pylint: disable=too-many-locals
        """
        The call method node signifies invocation of the forward method, this method extracts an Op representation of
        the module or alist of Ops in case of module representing a sub-graph. Typically the node has the following construct:
            %output_N : Tensor = prim::CallMethod[name="forward"](%output_L, %output_M)
        :param node: trace graph node i.e. 'CallMethod' node
        :param model: Pytorch model to create connected graph from
        :param trace: trace of model or submodule
        :param module_tensor_tuples_map: Map of modules and input and output tensor obtained from a forward pass
        :param node_name_to_module: dictionary of module indexed by output_name referenced in the sub-graph
        :param ops: dictionary of Ops and Products indexed by output names referenced in the graph
        :param node_name_to_subgraph_model: dictionary of torch graph nodes index of output_name that have not been resolved.
        """
        output_name: str = node.output().debugName()
        inputs = self._resolve_input_nodes(node)
        # 1st input is a reference on which the call method is being invoked.
        input_name: str = inputs[0].debugName()
        if input_name in node_name_to_subgraph_model:
            input_ops = [ops[i.debugName()] for i in inputs[1:]]

            subgraph_model = node_name_to_subgraph_model[input_name]
            # The trace and subgraph_model might not be at the same depth depending on the number of indirection
            # used in the form of ModuleList or Sequential,
            # the trace is traversed one level at a time to allow for accessing the instance at each level.
            trace_level = self._module_to_name[subgraph_model].replace(self._module_to_name[model], '')[1:].split('.')
            subgraph_trace = trace
            for level in trace_level:
                subgraph_trace = getattr(subgraph_trace, level)

            # the op returned on parsing the sub-graph shall be last op in the sub-graph forward pass
            ops[output_name] = self._parse_trace_graph(subgraph_trace, subgraph_model, input_ops,
                                                       module_tensor_tuples_map)
        if input_name in node_name_to_module and is_leaf_module(node_name_to_module[input_name]):
            # the graph is fully represented by a directional graph of leaf torch modules so the recursion is
            # stopped at this level. PassThroughOp are being ignored because in graph node representation
            # the passthrough op generate no output and are not part of inputs for downstream op
            if not isinstance(node_name_to_module[input_name], PassThroughOp):
                ops[output_name] = self._create_leaf_module_op(node_name_to_module[input_name],
                                                               inputs, ops, module_tensor_tuples_map)

    def _create_input_products(self, model_input: Tuple[torch.Tensor], graph: torch._C.Graph) -> \
            Tuple[str, Dict[str, Union[Op, Product]]]:
        # pylint: disable=protected-access
        """
        Creates a dictionary of input products index by input name referenced in the sub-graph
        :param model_input: Example input to model.  Can be a single tensor or a list/tuple of input tensors
        :param graph: trace graph representing the model or sub-set of the model
        :return: model input name , dictionary of input products
        """
        products = {}
        model_debug_name = None
        for input_index, inp in enumerate(graph.inputs()):
            input_name: str = inp.debugName()
            if input_index == 0:
                model_debug_name = input_name
            else:
                inp_op = model_input[input_index - 1]
                if isinstance(inp_op, torch.Tensor):
                    shape = list(inp_op.shape)
                    self._parameters[input_name] = (None, 'input', shape)
                    product = Product(input_name, shape)
                    product.is_model_input = True
                    self._products[product.name] = product
                    products[input_name] = product
                elif isinstance(inp_op, tuple([Product, Op])):
                    products[input_name] = inp_op
                else:
                    logger.warning("ignoring input %s of unknown type %s", str(inp_op), type(inp_op))
        assert model_debug_name is not None
        return model_debug_name, products

    @staticmethod
    def _get_attribute_name(node: torch._C.Node) -> Dict[str, str]:
        # pylint: disable=protected-access
        """
        Retrieve the attributes associated with the graph node
        :param node: trace graph node
        :return: a dictionary of attributes associated with the node
        """
        attributes = {}
        # node description has pseudo-code of the form  '... torch_mangle_2.Module = prim::GetAttr[name="fc"](%self.1)'
        # for the above example attributeNames() iterator should return a string 'name'
        node_desc = str(node)
        for attribute_name in node.attributeNames():
            pattern = attribute_name + '="'
            if pattern in node_desc:
                attributes[attribute_name] = node_desc.split(pattern)[1].split('"')[0]
        return attributes

    @staticmethod
    def _get_module_instance(node: torch._C.Node,
                             node_name_to_module: Dict[str, torch.nn.Module]) -> torch.nn.Module:
        # pylint: disable=protected-access
        """
        Get the torch.nn.Module referenced by the node.
        :param node: trace graph node
        :param node_name_to_module: dictionary of module index by output_name referenced in the sub-graph
        :return: list of attributes defined with the node
        """
        input_name: str = node.input().debugName()
        attributes = ConnectedGraph._get_attribute_name(node)
        model = node_name_to_module[input_name]
        sub_model = getattr(model, attributes['name'])
        return sub_model

    def _resolve_input_nodes(self, node: torch._C.Node) -> List[torch._C.Node]:
        # pylint: disable=protected-access
        """
        recursively aggregate inputs nodes that produce inputs consumed by the node
        recursion is used to aggregate inputs feeding into the input_node if the node belongs to a ignored node list.
        :param node: trace graph node
        :return: list of producer nodes that feed the node
        """
        inputs = []
        for _, inp in enumerate(node.inputs()):
            if inp.node().kind() in self.ignore_graph_nodes:
                inputs.extend(self._resolve_input_nodes(inp.node()))
            else:
                inputs.append(inp)
        return inputs

    def _create_leaf_module_op(self, model: torch.nn.Module,
                               inputs: List[Union[torch._C.Node, torch._C.Value]], ops: Dict[str, Union[Op, Product]],
                               module_tensor_tuples_map: Dict[str, Tuple[Tuple[torch.Tensor]]]) -> Op:
        # pylint: disable=protected-access
        """
        Creates a fully populated Op and along with associated products representing inputs
        :param model: PyTorch Module representing the model or a sub-set of the model
        :param inputs: list of producer graph nodes or graph input values
        :param ops: dictionary of Ops and Products indexed by output names referenced in the graph
        :return: Op
        """
        # use nominal Op type if its a known type else use torch defined Module name
        if isinstance(model, tuple(self.op_type_map.keys())):
            op_type = self.op_type_map[type(model)]
        else:
            op_type = type(model).__name__
            logger.info("unknown op_type -- defaulting to class name %s", op_type)

        # inputs[0] refers to the module instance, the tensor inputs starts from 1.
        op = self._create_op_and_products(op_type, inputs[1:], ops)

        # populating module info associated with Op
        op.model_module = PytorchModelModule(model)
        self._module_to_op_dict[model] = op
        op.dotted_name = self._module_to_name[op.get_module()]
        _fill_conv_op_info(op, model)

        # populating input and output shapes from xxx_tensor_tuple obtained via hook and forward pass
        input_tensor_tuple, output_tensor_tuple = module_tensor_tuples_map[model]
        # xxx_tensor_tuple is a union(Tensor, tuple(Tensor)), obtain the shape of the Tensor (or first Tensor if Tuple)
        output_shape = list(
            output_tensor_tuple[0].shape if isinstance(output_tensor_tuple, tuple) else output_tensor_tuple.shape)
        input_shape = list(
            input_tensor_tuple[0].shape if isinstance(input_tensor_tuple, tuple) else input_tensor_tuple.shape)
        _fill_and_check_op_product_shapes(op, input_shape, output_shape)
        return op

    def _create_functional_op(self, node: torch._C.Node, ops: Dict[str, Union[Op, Product]]) -> Op:
        # pylint: disable=protected-access
        """
        Creates an Op and along with associated products representing inputs. If output shape is available then shapes
        attribute are populated as well.
        :param node: trace graph node
        :param ops: dictionary of Ops and Products indexed by output names referenced in the graph
        :return: Op
        """
        inputs = self._resolve_input_nodes(node)

        op_type = ConnectedGraph._parse_op_type(node)
        op = self._create_op_and_products(op_type, inputs, ops)

        # Determine the output_shape based on output_type
        output_type = node.output().type()
        if isinstance(output_type, torch._C.TensorType):
            output_shape = list(output_type.sizes())
        elif isinstance(output_type, torch._C.TupleType) and \
                isinstance(output_type.elements()[0], torch._C.TensorType) and \
                output_type.elements()[0].sizes() is not None:
            # the first output_shape is assumed to define the shape of the output tensor
            output_shape = list(output_type.elements()[0].sizes())
        else:
            # output is not of a Tensor type e.g. Int, skip setting shape fpr this op
            return op

        # first input tensor is assumed to define the shape of the input tensor(s)
        inp_op = ops[inputs[0].debugName()]
        if isinstance(inp_op, Product):
            input_shape = inp_op.shape
        else:
            input_shape = inp_op.output_shape
        _fill_and_check_op_product_shapes(op, input_shape, output_shape)
        return op

    def _create_op_and_products(self, op_type: str, inputs: List[Union[torch._C.Node, torch._C.Value]],
                                ops: Dict[str, Union[Op, Product]]) -> Op:
        # pylint: disable=protected-access
        """
        Creates an Op and along with associated products representing inputs.
        :param op_type: string representation fo Op type could be one of the following:-
            > normalized name if mapping exists in op_type_map
            > class name if associated with an instance of torch.nn.Module
            > string extracted from graph node description in case of functional Op
        :param inputs: list of producer graph nodes or graph input values
        :param ops: dictionary of Ops and Products indexed by output names referenced in the graph
        :return: Op
        """
        unique_op_name = self._make_unique_op_name(op_type)
        op = Op(name=unique_op_name, dotted_name=unique_op_name, output_shape=None, is_anonymous=False, op_type=op_type)
        self.ordered_ops.append(op)
        self._ops[unique_op_name] = op
        for inp in inputs:
            input_name = inp.debugName()
            resolved_inp = ops[input_name]
            if isinstance(resolved_inp, Op):
                # Create a product linking the identified Operation with the current Operation.
                self._create_and_link_inter_op_product(resolved_inp, op)
            elif isinstance(resolved_inp, Product):
                self._associate_op_with_parameter_product(op, resolved_inp)
        return op

    def _make_unique_op_name(self, op_name: str) -> str:
        """
        Given an op name, combine it with the self._op_count member to create a unique op name.  Increment
        self._op_count each time.
        :param op_name: Name of the operation to create a unique op name from.
        :return: The unique op name.
        """
        unique_op_name = op_name + '_' + str(self._op_count)
        self._op_count += 1
        return unique_op_name

    def _create_and_link_inter_op_product(self, parent_op: Op, current_op: Op):
        """
        Given a parent op and child op, create a product to link the two if it doesn't yet exist.
        :param parent_op: The parent op.
        :param current_op: The child op.
        """
        product_name = parent_op.name + '_to_' + current_op.name
        input_product = self.get_product(product_name)
        if not input_product:
            input_product = Product(product_name, parent_op.output_shape)
            self._products[input_product.name] = input_product
        parent_op.output = input_product
        input_product.producer = parent_op
        input_product.add_consumer(current_op)
        current_op.add_input(input_product)

    def _associate_op_with_parameter_product(self, op: Op, parameter_product: Product):
        """
        Given an op and a Product that represents a parameter of the op, link the op to the product.
        :param op: The op to link the parameter product to.
        :param parameter_product: The parameter product associated with the op.
        """
        if not op.model_module:
            param_tuple = self._parameters[parameter_product.name]
            if param_tuple[0]:
                op.model_module = PytorchModelModule(param_tuple[0])
        parameter_product.add_consumer(op)
        op.add_input(parameter_product)

    def _remove_tuple_ops(self):
        """
        Removes Op and Products related to TupleConstruct and TupleUnpack and creates new products to directly
        bind TupleConstruct producers  with TupleUnpack consumers
        """
        # TODO remove the below pylint suppression
        # pylint: disable=too-many-locals
        remove_ops = []
        remove_products = []
        for op in self.ordered_ops:
            if op.type == 'TupleConstruct':
                remove_ops.append(op)
                pack_producers = [p.name for p in op.inputs]
                pack_consumers = [k for k, v in self._products.items() if v.producer == op]

                # sorting TupleConstruct_X_to_TupleUnpack_Y1, TupleConstruct_X_to_TupleUnpack_Y2 etc
                # Y1, Y2, .. are in the order of TupleUnpack Ops construction i.e. in the order of tuple tensor
                # TODO use an alternate scheme to order the consumer products instead of names
                pack_consumers.sort(key=lambda name: int(re.findall(r'\d+', name)[-1]))

                # no consumer for TupleConstruct Op, likely due to being the final output of the model
                if not pack_consumers:
                    remove_products.extend(pack_producers)
                    continue

                assert len(pack_consumers) == len(pack_producers)
                for pack_producer, pack_consumer in zip(pack_producers, pack_consumers):

                    unpack_op = self._products[pack_consumer].consumers
                    assert len(unpack_op) == 1
                    assert unpack_op[0].type == 'TupleUnpack'
                    unpack_op = unpack_op[0]

                    remove_products.extend([pack_producer, pack_consumer])
                    remove_ops.append(unpack_op)

                    producer_product = self._products[pack_producer]
                    producer_op = producer_product.producer
                    unpack_consumers_product = [k for k, v in self._products.items() if v.producer == unpack_op]

                    # unpack_consumers is empty if the unpacked tensor is not part of subsequent forward pass
                    if not unpack_consumers_product:
                        producer_op.output = None
                        continue

                    remove_products.extend(unpack_consumers_product)
                    for unpack_consumer_product in unpack_consumers_product:
                        for unpack_consumer in self._products[unpack_consumer_product].consumers:
                            # Create a new product to replace the 'unpack' product
                            product_name = producer_op.name + '_to_' + unpack_consumer.name
                            input_product = Product(product_name, producer_op.output_shape)
                            self._products[input_product.name] = input_product
                            producer_op.output = input_product
                            input_product.producer = producer_op
                            input_product.add_consumer(unpack_consumer)

                            # replace 'unpack' product with new product
                            unpack_consumer.inputs = [input_product if unpack_op == inp.producer else inp
                                                      for inp in unpack_consumer.inputs]

        for k in remove_products:
            self._products.pop(k)
        for op in remove_ops:
            self.ordered_ops.remove(op)
            self._ops.pop(op.name)

    def _fill_op_params(self):
        """
        For certain ops like convolution, batch norm, and linear, create products for their parameters if they don't
        exist yet.
        """
        for op in self._ops.values():
            module = op.get_module()
            name = self._module_to_name.get(module, None)
            if op.type in ['convolution', 'batch_norm', 'addmm', 'matmul']:
                if module.weight is not None:
                    product_name = name + '.weight'
                    self._create_and_add_param_product_if_not_exists(op, product_name, list(module.weight.shape))
                if module.bias is not None:
                    product_name = name + '.bias'
                    self._create_and_add_param_product_if_not_exists(op, product_name, list(module.bias.shape))
            if op.type == 'batch_norm':
                # If batch_norm, fill in rest of bn params
                if module.running_mean is not None:
                    product_name = name + '.running_mean'
                    self._create_and_add_param_product_if_not_exists(op, product_name, list(module.running_mean.shape))
                if module.running_var is not None:
                    product_name = name + '.running_var'
                    self._create_and_add_param_product_if_not_exists(op, product_name, list(module.running_var.shape))

    def _create_and_add_param_product_if_not_exists(self, op: Op, product_name: str, shape):
        """
        Given a name of a product, create it if it doesn't exist, and attach it to the specified op as a parameter.
        :param op: Op to connect the parameter product to.
        :param product_name: Name of the product to create.
        :param shape: Shape of the product to create.
        """
        if product_name not in self._products.keys():
            product = Product(product_name, shape)
            product.is_parm = True
            product.add_consumer(op)
            op.add_input(product)
            self._products[product_name] = product

    def _determine_split_behavior_for_op_and_insert_split_op_in_connected_graph(self, op: Op):
        """
        Determine if an Op's output is used as an input to more than one Op. If it is, create a Split Op and
        insert it in the connected graph, below this Op.
        Note that the split is done in the forward() function of a model and is NOT a PyTorch OP.
        :param op: Op to check if output is used as an input to more than one op.
        """

        name = op.name
        dotted_name = op.dotted_name

        # Get the output product names.
        output_product_names = self.get_product_names_from_dotted_name(dotted_name)

        name_list = []
        for prod_name in output_product_names:
            to_pos = prod_name.find('to')
            first_name = prod_name[:to_pos]
            name_list.append(first_name)

        # Split ops have 2 or more output products
        if len(output_product_names) > 1:
            name_list = [+1 for prod in name_list if name in prod]
            if len(name_list) > 1:
                logger.debug("%s is a split Op", op.dotted_name)

                # Create a Split Op
                split_op = self._create_split_op(op)

                # Insert the Split Op in the connected graph.
                self._insert_split_op_in_connected_graph(op, split_op)

    def get_product_names_from_dotted_name(self, dotted_name: str) -> List[str]:
        """
        Returns all names of products whose producer op dotted name matches the argument dotted name.
        For Residual models, same producer will have multiple products.
        During connected graph construction, only one output product can be associated with an op, so previous output
        products are overwritten when a new op is created.  Thus we must search through products dictionary for all
        output products corresponding to an op.
        :param dotted_name: Dotted name for connected graph op to check for output products.
        :return: List of products
        """

        matched_products = list()
        for product in self._products.values():
            if product.producer:
                if product.producer.dotted_name == dotted_name:
                    matched_products.append(product.name)
        return matched_products

    def _create_split_op(self, op: Op) -> Op:
        """
        The op's output is split in the forward function. To model it correctly, create a Split Op.
        :param op: Op to create split op after
        :return: Split op that was created
        """
        split_name_parts = ['Split_', str(self._split_count)]
        split_name = ''.join(split_name_parts)
        self._split_count += 1
        split_dotted_name_parts = [self._model_name, split_name]
        split_dotted_name = '.'.join(split_dotted_name_parts)
        is_anonymous = True
        split_op = Op(name=split_name, dotted_name=split_dotted_name, output_shape=op.output_shape,
                      is_anonymous=is_anonymous, op_type='Split')
        self._ops[split_name] = split_op
        return split_op

    def _insert_split_op_in_connected_graph(self, preceding_op: Op, split_op: Op):
        """
        Insert a Split Op below the preceding Op in the connected graph.
        :param preceding_op: Op prior to split op
        :param split_op: Split op to insert
        """

        # Important Notes
        # Op:
        # An Op class represents a module in a model.
        #
        # Product:
        # In this version of the Winnower, the Product class represents the following entities in a model.
        # 1) a Tensor between two modules (in Winnower, 2 Ops).
        # 2) an input
        # 3) a constant
        # 4) a parameter
        #
        # Considering only the definition #1) above, i.e., Product is a Tensor between 2 Ops,
        # an Op's inputs and output are Products.
        # That means, an Op could have multiple input Products and one output Product.
        # Examples of Op with multiple input products: add, cat (Concat)
        # A Product's Producer and Consumer are Ops.
        # A Product could have only one Producer but could have multiple consumers.
        # For example, a Split Op has one output.  The Split Op's single output isa Product.
        # That product's single Producer is the Split Op and multiple consumers are the 2 Ops in the 2 branches of
        # the Split, that receive the Split output.

        # Steps:
        # 1. Create a new Product for Split Op's output.
        # 2.This product has multiple consumers. Add the consumers to the Product.
        #   Get the consumers from the op's multiple products.
        # 3. Set the the current Op's output Product's consumer to Split Op. The output product's name must be changed.
        # 4. Set the Split Op's input to point to current Op's output. Its name must be changed.

        # 1. Create a new Product for Split Op's output.
        split_op_product = self._create_split_op_output_product(preceding_op, split_op)
        split_op.output = split_op_product

        # 2.This product has multiple consumers. Add the consumers to the Product.
        # Get the consumers from the op's multiple products.

        self._add_consumers_to_split_op_product(preceding_op, split_op_product)

        # 3. Create a new product to connect the preceding Op to the Split Op.
        # Set the the preceding Op's output Product's consumer to Split Op.

        # The preceding Op's output products (products, since it was behaving like a Split) are going to be deleted,
        # since a Split is being inserted in the connected graph.
        # Save the preceding Op's output Product shape.
        # This is needed to create the new product from the preceding Op to the newly being inserted Split Op.
        new_product_shape = preceding_op.output.shape

        # Since the preceding Op was behaving like a Split Op, it  would have 2 products with the preceding Op as the
        # producer. Delete these products from the product dictionary.
        preceding_op_product_names = self.get_product_names_from_dotted_name(preceding_op.dotted_name)
        for name in preceding_op_product_names:
            # Important Notes
            # The following check is needed since ResNet uses the same Relu twice in BasicBlock's forward()
            # Please read the details comments in _add_consumers_to_split_op_product()
            if preceding_op.name in name:
                deleted_product = self._products.pop(name)
                logger.debug("Insert Split Op: Step 3. Deleted product: %s", deleted_product)

        new_product_name = preceding_op.name + '__to__' + split_op.name
        new_product_shape = preceding_op.output.shape
        new_product = self._add_product(new_product_name, new_product_shape)
        new_product.producer = preceding_op
        preceding_op.output = new_product
        preceding_op.output.consumers.append(split_op)

        # 4. Set the Split Op's input to point to current Op's output.
        # new_name = preceding_op.name + '__to__' + split_op.name
        split_op.inputs.append(preceding_op.output)

    def _create_split_op_output_product(self, preceding_op: Op, split_op: Op) -> Product:
        """
        Create output product of the split op and connected it to the split op
        :param preceding_op: Op prior to split op
        :param split_op: Split op to create output product for
        :return: Output product of the split op
        """
        split_op_product_name = split_op.name + '__to__' + 'multiple_ops'
        split_op_product_shape = preceding_op.output.shape
        split_op_product = self._add_product(split_op_product_name, split_op_product_shape)
        split_op_product.producer = split_op
        return split_op_product

    def _add_product(self, name: str, shape: List[int]) -> Product:
        """
        Add product to self._products dictionary
        :param name: Name of product
        :param shape: Shape of product
        :return: Product that was created
        """
        assert name not in self._products
        product = Product(name, shape)
        self._products[name] = product
        return product

    def _add_consumers_to_split_op_product(self, preceding_op: Op, split_op_product: Product):
        """
        A Split Op's output product has multiple consumers. Add them to the product.
        :param preceding_op: Op prior to split op
        :param split_op_product: Output product of split op
        """

        dotted_name = preceding_op.dotted_name
        output_product_names = self.get_product_names_from_dotted_name(dotted_name)

        # Important Notes
        # ResNet model uses the same Relu twice in the forward function of ResNet's BasicBlock.
        # The first Relu feeds in to the BasicBlock's Conv2.
        # The second Relu's output is split with one branch feeding the next BasicBlock's conv1 and the other
        # branch feeding in to the next BasicBlock's Add.
        # The following line filters out the Relu whose output is NOT split :(
        out_product_names = [name for name in output_product_names if preceding_op.name in name]

        num_products = len(out_product_names)
        consumer_index = 0
        for a_product_index in range(num_products):
            a_product = self.get_product(out_product_names[a_product_index])
            a_consumer = a_product.consumers[0]
            split_op_product.consumers.append(a_consumer)
            logger.debug("Insert Split Op: Step 2a. Consumer Op: %s, a_product_index: %s",
                         a_consumer.dotted_name, a_product_index)
            if a_consumer.type in ('cat', 'add'):
                # Need to insert the newly created split_op product in the correct input index of the cat Op :)
                logger.debug("Insert Split Op: Step 2b. Op has multiple input products: %s", a_consumer.inputs)
                input_product_index = determine_preceding_op_input_product_index_in_multi_input_op(preceding_op,
                                                                                                   a_consumer)
                a_consumer.inputs[input_product_index] = split_op_product
                logger.debug("Insert Split Op: Step 2c. For product: %s, split_op input_product_index: %s",
                             split_op_product.name, input_product_index)
            else:
                # There is only one input to this consumer. Add it to the 0th index of inputs.
                logger.debug("Insert Split Op: Step 2d. Op has single input product: %s", a_consumer.inputs)
                input_product_index = 0
                a_consumer.inputs[input_product_index] = split_op_product
                logger.debug("Insert Split Op: Step 2e. For split_op product: %s, input_product_index: %s",
                             split_op_product.name, input_product_index)
            consumer_index += 1

    def _fill_empty_shapes(self):
        """ Anonymous ops like add or concat do not have shapes associated with them when their ops are created.
        Traverse through ops in the graph in order and use existing product shape information to try to infer what the
        shapes of ops and products with missing shapes are.  This may not be completely accurate in the face of reshape
        and unknown ops. """

        starting_ops = []
        for product in self.get_all_products().values():
            if product.is_model_input:
                for consumer in product.consumers:
                    if consumer not in starting_ops:
                        starting_ops.append(consumer)

        ops_in_order = get_ordered_ops(starting_ops)
        for op in ops_in_order:
            for inp in op.inputs:
                assert inp.shape is not None

            # If op's output product has a shape already, simply use that shape as op's output_shape as well.
            if op.output_shape is None:
                if op.output and op.output.shape is not None:
                    op.output_shape = op.output.shape
                    continue

                # If op is of type cat, add the number of channels of incoming products and declare that is the number
                # of out channels in the outgoing product.
                if op.type == 'cat':
                    num_channels = 0
                    for inp in op.inputs:
                        num_channels += inp.shape[api_channel_index_dict[ModelApi.pytorch]]
                    concat_shape = op.inputs[0].shape
                    concat_shape[api_channel_index_dict[ModelApi.pytorch]] = num_channels
                    op.output_shape = concat_shape

                # Otherwise, assume that the input shape is unchanged (this may be wrong, but hopefully should not
                # matter in the scheme of mask propagation since ops that matter should not be anonymous).
                else:
                    op.output_shape = op.inputs[0].shape
                if op.output:
                    op.output.shape = op.output_shape

    # pylint: disable=too-many-lines
    def _validate_op_modules(self):
        """
        Utility function to ensure that all connected graph ops of a certain type have associated modules
        """
        missing_modules = []
        for op_name, op in self.get_all_ops().items():
            if not op.get_module() and op.type not in self.functional_ops:
                missing_modules.append(op_name)
        if missing_modules:
            # TODO: replace with logger.error and assertion after rewriting unit tests to avoid using built in vgg,
            #  resnet, and inception models (since they use functionals in their models)
            logger.warning('Ops with missing modules: %s\n'
                           'This can be due to several reasons:\n'
                           '1. There is no mapping for the op in ConnectedGraph.op_type_map. Add a mapping for '
                           'ConnectedGraph to recognize and be able to map the op.\n'
                           '2. The op is defined as a functional in the forward function, instead of as a class '
                           'module. Redefine the op as a class module if possible. Else, check 3.\n'
                           '3. This op is one that cannot be defined as a class module, but has not been added to '
                           'ConnectedGraph.functional_ops. Add to continue.'
                           , missing_modules)


def _fill_and_check_op_product_shapes(op: Op, input_shape: List, output_shape: List):
    """
    Given an Op and input and output shapes obtained from forward pass, fill in the shapes for the op and its input and
    output products.  If these products already have shapes associated with them, check that they match with the given
    shapes; if not, log an error.
    :param op: Current op to fill shape parameter
    :param input_shape: Input shape obtained from forward pass
    :param output_shape: Output shape obtained from forward pass
    """
    op.output_shape = output_shape
    for inp in op.inputs:
        if not inp.is_parm:
            if inp.shape and inp.shape[1:] != input_shape[1:]:
                logger.warning(
                    '[Deprecate?] Mismatch btw shape %s for product %s and input shape %s for input of op %s',
                    inp.shape, inp, input_shape, op)
            elif not inp.shape:
                inp.shape = input_shape
    if op.output:
        if op.output.shape and op.output.shape[1:] != output_shape[1:]:
            logger.error('Mismatch between existing shape %s for product %s and output shape %s for output of op %s',
                         op.output.shape, output_shape, input_shape, op)
        elif not op.output.shape:
            op.output.shape = op.output_shape


def _fill_conv_op_info(op: Op, module: torch.nn.Module):
    """ Fill in groups info """

    if op.type in 'convolution':
        op.groups = module.groups
