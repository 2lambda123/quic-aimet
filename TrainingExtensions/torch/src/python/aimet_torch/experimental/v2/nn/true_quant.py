# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2024, Qualcomm Innovation Center, Inc. All rights reserved.
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
# =============================================================================
""" True-quantized modules"""

import contextlib
import itertools
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Type, Any, List, Callable, Protocol, Tuple, Union, Sequence, Dict, Optional

import torch
import torch.nn as nn
import torch.utils._pytree as pytree
from torch import Tensor

from aimet_torch.experimental.v2.nn.quant_base import BaseQuantizationMixin
from aimet_torch.experimental.v2.quantization.quantizers.base import QuantizerBase
from aimet_torch.experimental.v2.utils import patch_attr

OpArgs = Any
_CURRENT_TRUE_QUANT_BACKEND = []


class _QuantizedOpLibrary(Protocol):
    """
    Protocol for integer operator libraries to follow for AIMET compatibility
    """

    @staticmethod
    def get_kernel(op_key: str) -> Sequence[Tuple[Callable[[OpArgs], bool], Callable[[OpArgs], Any]]]:
        """
        Takes the kernel name as an argument and returns a sequence of (predicate, operator) pairs which take identical
        arguments. The predicate function will return True if the operator can be successfully called with the given
        inputs, False otherwise.
        """


def set_default_true_quant_backend(backends: Union[List[_QuantizedOpLibrary], _QuantizedOpLibrary]):
    """
    Set the default operator library(s0) for true-quantized modules
    """
    if not isinstance(backends, (list, tuple)):
        backends = [backends]
    global _CURRENT_TRUE_QUANT_BACKEND # pylint:disable = global-statement
    _CURRENT_TRUE_QUANT_BACKEND = backends


def get_true_quant_backend() -> List[_QuantizedOpLibrary]:
    """
    Get the current default true-quant operator libraries
    """
    return _CURRENT_TRUE_QUANT_BACKEND.copy()


# pylint:disable = protected-access
pytree._register_pytree_node(torch.nn.ModuleList,
                             pytree._list_flatten,
                             pytree._list_unflatten)

pytree._register_pytree_node(torch.nn.ModuleDict,
                             pytree._dict_flatten,
                             pytree._dict_unflatten)


def _maybe_quantize(data: Any, quantizer: Optional[QuantizerBase]):
    """
    Quantize data if it is a quantizable type and quantize is not None
    """
    if quantizer and isinstance(data, Tensor) and data.is_floating_point():
        return quantizer(data)
    return data


def _tree_map(fn: Callable, tree: pytree.PyTree, *others: pytree.PyTree):
    leaves, spec = pytree.tree_flatten(tree)
    others = [pytree.tree_flatten(other)[0] for other in others]
    return pytree.tree_unflatten(list(map(fn, leaves, *others)), spec)


class TrueQuantizationMixin(BaseQuantizationMixin, ABC):
    """
    Mixin that allows dispatch to quantized operator libraries in place of native pytorch operations
    """

    cls_to_qcls = OrderedDict()  # quantized class -> original class
    qcls_to_cls = OrderedDict()  # original class -> quantized class
    op_key: str
    allow_backend_fallback: bool
    allow_float_fallback: bool
    _backends: List[_QuantizedOpLibrary]
    _backend_kwargs: Dict[_QuantizedOpLibrary, Dict[str, Any]]
    quantized_classes_map = OrderedDict()

    def __init__(self,
                 *args,
                 backend: Union[_QuantizedOpLibrary, List[_QuantizedOpLibrary]] = None,
                 float_fallback: bool = False,
                 backend_fallback: bool = False,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.set_backend(backend, backend_fallback)
        self._backend_kwargs = {}
        self.allow_float_fallback = float_fallback

    @contextlib.contextmanager
    def compute_encodings(self):
        def no_op(tensor_in: Tensor):
            return tensor_in

        with contextlib.ExitStack() as stack:
            for quantizer in itertools.chain(self.input_quantizers, self.output_quantizers):
                if not quantizer:
                    continue
                # NOTE: This behavior is for backawrd-compatibility with V1 quantsim.
                stack.enter_context(patch_attr(quantizer, 'forward', no_op))
            stack.enter_context(patch_attr(self, "call_operator", self.fallback_operator))
            with super().compute_encodings():
                yield

    def available_backends(self) -> List[_QuantizedOpLibrary]:
        """
        Retrieve all operator libraries available to the layer
        """
        if self.allow_backend_fallback or not self._backends:
            return self._backends + get_true_quant_backend()
        return self._backends.copy()

    def set_backend(self,
                    backend: Union[List[_QuantizedOpLibrary], _QuantizedOpLibrary],
                    allow_fallback=False):
        """
        Set the layer's operator library

        :param backend: library or list of libraries to call into
        :param allow_fallback: If True, allow fallback to default operator libraries
        """
        if backend is None:
            backend = []
        self.allow_backend_fallback = allow_fallback
        if not isinstance(backend, (list, tuple)):
            backend = [backend]
        self._backends = backend

    def set_backend_kwargs(self, backend: _QuantizedOpLibrary, **kwargs):
        """
        Sets additional keyword arguments to pass to the specified backend when selected

        :param backend: the operator library for which to add keyword arguments
        """
        self._backend_kwargs[backend] = kwargs

    def get_backend_kwargs(self, backend) -> Dict[str, Any]:
        """
        Retrieves the keyword arguments for the specified backend

        :param backend: Backend to retrieve keyword arguments for
        """
        return self._backend_kwargs.get(backend, {})

    def select_operator(self, args, kwargs) -> Tuple[Callable, Tuple, Dict]:
        """
        Returns the first kernel (and kernel arguments) for which the predicate function returns True.
        Predicates are tested in the following order:
            1) First local backend --> last local backend
            2) (if self.allow_backend_fallback)  First global backend --> last global backend
            3) (if self.allow_float_fallback) Fake-quant forward pass

        :return: Tuple of operator, operator positional arguments, operator keyword arguments
        """
        op_args, op_kwargs = self.functional_op_arguments(*args, **kwargs)
        op_kwargs["output_encodings"] = pytree.tree_map_only(QuantizerBase, lambda q: q.get_encoding(), self.output_quantizer_tree())
        for backend in self.available_backends():
            backend_kwargs = self._add_backend_kwargs(backend, **kwargs)
            for predicate, operator in backend.get_kernel(self.op_key):
                if predicate(*op_args, **backend_kwargs):
                    return operator, op_args, backend_kwargs
        if self.allow_float_fallback:
            return self.fallback_operator, args, kwargs
        raise RuntimeError(f"No compatible operator found for function {self.op_key} in libraries "
                           f"{self.available_backends()} with input arguments: {op_args}, {op_kwargs}")

    @abstractmethod
    def functional_op_arguments(self, *args, **kwargs):
        """
        Return the args and kwargs needed to call the layer's functional operator.
        The ordering of args should match the torch.nn.functional equivalent function, and kwargs should be the same
        as the torch.nn.functional kwargs, with the addition of 'output_encodings'
        """

    def _add_backend_kwargs(self, backend, **kwargs):
        additional_kwargs = self.get_backend_kwargs(backend)
        kwargs.update(additional_kwargs)
        return kwargs

    def quantized_operator(self, *quantized_inputs, **kwargs):
        """
        Selects the first operator which can evaluate successfully and returns its output
        """
        operator, op_args, op_kwargs = self.select_operator(quantized_inputs, kwargs)
        return operator(*op_args, **op_kwargs)

    def fallback_operator(self, *quantized_inputs, **kwargs):
        """
        Implements the fake-quant fallback mechanism:
            1) All quantized tensors will be automatically dequantized in the super().forward() call
            2) The output(s) of super().forward() are quantized by mapping self.output_quantizer_tree()
               to the outputs
        """
        outputs = super().forward(*quantized_inputs, **kwargs)
        outputs = _tree_map(_maybe_quantize, outputs, self.output_quantizer_tree())
        return outputs

    def output_quantizer_tree(self):
        """
        Returns output quantizers as a nested structure with the same pattern as the layer outputs. In layers with
        multiple outputs, this defines both:
            1) which output quantizer will be applied to which output during fake-quant
            2) which output encoding corresponds to which output during true-quant

        such that these two cases cannot become misaligned.

        This must be overridden in the case that the structure of self.output_quantizers does not already match the
        structure of outputs.
        """
        return self.output_quantizers[0] if len(self.output_quantizers) == 1 else self.output_quantizers

    @classmethod
    def wrap(cls, module_cls: Type[nn.Module]) -> Type[nn.Module]:
        """
        Wrap a regular module class into a true-quantized module class
        """
        if not issubclass(module_cls, nn.Module):
            raise ValueError("Expected module_cls to be a subclass of torch.nn.Module. "
                             f"Got {module_cls}.")
        if module_cls in cls.cls_to_qcls:
            return cls.cls_to_qcls[module_cls]

        quantized_cls_name = f"TrueQuantized{module_cls.__name__}"
        base_classes = (cls, module_cls)
        quantized_cls = type(quantized_cls_name, base_classes, {'__module__': __name__})
        return cls.implements(module_cls)(quantized_cls)

    @classmethod
    def implements(cls, module_cls):
        """
        Decorator for registering true-quantized implementation of the given base class.
        """

        def wrapper(quantized_cls):
            cls.cls_to_qcls[module_cls] = quantized_cls
            cls.qcls_to_cls[quantized_cls] = module_cls
            return quantized_cls

        return wrapper


# pylint: disable=arguments-differ, abstract-method

class _TrueQuantizedUnaryOpMixin(TrueQuantizationMixin, ABC):

    def quantized_forward(self, x, *args, **kwargs):
        x = _maybe_quantize(x, self.input_quantizers[0])

        with self._patch_quantized_parameters():
            return self.quantized_operator(x, *args, **kwargs)


class _TrueQuantizedBinaryOpMixin(TrueQuantizationMixin, ABC):

    def __quant_init__(self):
        super().__quant_init__()
        self.input_quantizers = nn.ModuleList([None, None])

    def quantized_forward(self, x, y, *args, **kwargs):
        x = _maybe_quantize(x, self.input_quantizers[0])
        y = _maybe_quantize(y, self.input_quantizers[1])

        with self._patch_quantized_parameters():
            return self.quantized_operator(x, y, *args, **kwargs)


@TrueQuantizationMixin.implements(nn.Linear)
class TrueQuantizedLinear(_TrueQuantizedUnaryOpMixin, nn.Linear):
    """ True-quantized linear """
    op_key = "linear"

    def functional_op_arguments(self, input_tensor):
        return (input_tensor, self.weight), {"bias": self.bias}


@TrueQuantizationMixin.implements(nn.GELU)
class TrueQuantizedGelu(_TrueQuantizedUnaryOpMixin, nn.GELU):
    """ True-quantized Gelu """
    op_key = "gelu"

    def functional_op_arguments(self, input_tensor):
        return (input_tensor,), {"approximate": self.approximate}


@TrueQuantizationMixin.implements(nn.LayerNorm)
class TrueQuantizedLayerNorm(_TrueQuantizedUnaryOpMixin, nn.LayerNorm):
    """ True-quantized layernorm """
    op_key = "layer_norm"

    def functional_op_arguments(self, input_tensor):
        args = (input_tensor, self.normalized_shape)
        kwargs = {
            "weight": self.weight,
            "bias": self.bias,
            "eps": self.eps
        }
        return args, kwargs
