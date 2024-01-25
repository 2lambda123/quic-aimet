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
"""Fake-quantized modules"""

from collections import OrderedDict
import contextlib
import itertools
from typing import Type
import abc

import torch.nn as nn

from aimet_torch.experimental.v2.utils import patch_param


class _QuantizationMixin(abc.ABC):
    """
    Mixin that implements quantization on top of regular pytorch modules.
    """

    input_quantizers: nn.ModuleList
    output_quantizers: nn.ModuleList
    param_quantizers: nn.ModuleDict

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__quant_init__()

    def __quant_init__(self):
        """
        Initializer for quantized module. This method will be invoked right after __init__.
        """
        self._forward = self.forward
        self.forward = self.quantized_forward

        self.param_quantizers = nn.ModuleDict({
            name: None for name, _ in self.named_parameters(recurse=False)
        })
        # Currently assume single input & output
        self.input_quantizers = nn.ModuleList([None])
        self.output_quantizers = nn.ModuleList([None])

    def quantized_forward(self, *inputs, **kwargs):
        """
        Forward function for quantized module.
        This method will replace the original forward function.
        """
        # Compute parameter encodings if not already computed
        self._compute_param_encodings(overwrite=False)

        quantized_inputs = _nested_map(inputs, self.input_quantizers)

        with self._patch_quantized_parameters():
            outputs = self._forward(*quantized_inputs, **kwargs)

        if not isinstance(outputs, (list, tuple)):
            outputs = (outputs,)

        quantized_outputs = _nested_map(outputs, self.output_quantizers)

        if len(quantized_outputs) == 1:
            quantized_outputs = quantized_outputs[0]

        return quantized_outputs

    @contextlib.contextmanager
    def _patch_quantized_parameters(self):
        with contextlib.ExitStack() as stack:
            for param_name, param_quantizer in self.param_quantizers.items():
                if param_quantizer:
                    orig_param = getattr(self, param_name)
                    quantized_param = param_quantizer(orig_param)
                    ctx = patch_param(self, param_name, quantized_param)
                    stack.enter_context(ctx)
            yield

    def _compute_param_encodings(self, overwrite: bool):
        for param_name, param_quantizer in self.param_quantizers.items():
            if not param_quantizer:
                continue

            if not param_quantizer.is_initialized() or overwrite:
                param = getattr(self, param_name)
                if param is not None:
                    with param_quantizer.compute_encodings():
                        _ = param_quantizer(param)

    @contextlib.contextmanager
    def compute_encodings(self):
        """
        Observe inputs and update quantization parameters based on the input statistics.
        During ``compute_encodings`` is enabled, the input/output quantizers will forward perform
        dynamic quantization using the batch statistics.
        """
        self._compute_param_encodings(overwrite=True)

        with contextlib.ExitStack() as stack:
            for quantizer in itertools.chain(self.input_quantizers, self.output_quantizers):
                if not quantizer:
                    continue
                ctx = quantizer.compute_encodings()
                stack.enter_context(ctx)
            yield

    @classmethod
    @abc.abstractmethod
    def wrap(cls, module_cls: Type[nn.Module]):
        """
        Wrap a regular module class into a quantized module class
        """

    @classmethod
    def from_module(cls, module: nn.Module):
        """
        Create an instance of quantized module from a regular moudle instance
        """
        # pylint: disable=protected-access
        module_cls = type(module)
        qtzn_module_cls = cls.quantized_classes_map.get(module_cls, None)

        if not qtzn_module_cls:
            raise RuntimeError(
                f'The quantized module definition of {module_cls} is not registered. '
                f'Please register the quantized module definition of {module_cls} '
                f'using `@{cls.__name__}.implements({module_cls.__name__})` decorator.'
            )

        qtzn_module = cls.__new__(qtzn_module_cls)

        qtzn_module.__dict__ = module.__dict__.copy()
        qtzn_module._modules = module._modules.copy()
        qtzn_module._parameters = module._parameters.copy()
        qtzn_module._buffers = module._buffers.copy()

        qtzn_module.__quant_init__()
        return qtzn_module


class FakeQuantizationMixin(_QuantizationMixin):
    """
    Mixin that implements fake-quantization on top of regular pytorch modules.
    """

    # Mapping from a base module class to quantized module class
    quantized_classes_map = OrderedDict()

    @classmethod
    def wrap(cls, module_cls: Type[nn.Module]) -> Type[nn.Module]:
        """
        Wrap a regular module class into a fake-quantized module class
        """
        if not issubclass(module_cls, nn.Module):
            raise ValueError("Expected module_cls to be a subclass of torch.nn.Module. "
                             f"Got {module_cls}.")
        if module_cls in cls.quantized_classes_map:
            return cls.quantized_classes_map[module_cls]

        quantized_cls_name = f"FakeQuant{module_cls.__name__}"
        base_classes = (FakeQuantizationMixin, module_cls)
        quantized_cls = type(quantized_cls_name, base_classes, {'__module__': __name__})
        return cls.implements(module_cls)(quantized_cls)

    @classmethod
    def implements(cls, module_cls):
        """
        Decorator for registering fake-quantized implementation of the given base class.
        """
        def wrapper(quantized_cls):
            cls.quantized_classes_map[module_cls] = quantized_cls
            return quantized_cls
        return wrapper


def _nested_map(nested_args, nested_fn):
    """
    Apply functions in a nested manner.
    The arguments and functions should share the same nested structure.
    For example,
      nested_args: {'arg0': tensor0, 'foo': [tensor1, tensor2]}
      nested_fn: {'arg0': f0, 'foo': [f1, f2]}
      output: {'arg0': f0(tensor0), 'foo': [f1(tensor1), f2(tensor2)]}
    """
    if nested_fn is None:
        return nested_args

    if isinstance(nested_fn, nn.ModuleList):
        nested_fn = list(nested_fn)

    if isinstance(nested_fn, nn.ModuleDict):
        nested_fn = dict(nested_fn)

    if isinstance(nested_args, (tuple, list)):
        if not isinstance(nested_fn, (tuple, list)):
            raise RuntimeError
        return [_nested_map(arg, fn) for arg, fn in zip(nested_args, nested_fn)]

    if isinstance(nested_args, dict):
        if not isinstance(nested_fn, dict):
            raise RuntimeError
        if nested_args.keys() - nested_fn.keys():
            raise RuntimeError
        return {
            key: _nested_map(nested_args[key], nested_fn[key])
            for key in nested_args
        }

    if not isinstance(nested_args, (tuple, list)):
        nested_args = (nested_args,)

    return nested_fn(*nested_args)


FakeQuantizedAdaptiveAvgPool1d = FakeQuantizationMixin.wrap(nn.AdaptiveAvgPool1d)
FakeQuantizedAdaptiveAvgPool2d = FakeQuantizationMixin.wrap(nn.AdaptiveAvgPool2d)
FakeQuantizedAdaptiveAvgPool3d = FakeQuantizationMixin.wrap(nn.AdaptiveAvgPool3d)
# FakeQuantizedAdaptiveLogSoftmaxWithLoss = FakeQuantizationMixin.wrap(nn.AdaptiveLogSoftmaxWithLoss)
FakeQuantizedAdaptiveMaxPool1d = FakeQuantizationMixin.wrap(nn.AdaptiveMaxPool1d)
FakeQuantizedAdaptiveMaxPool2d = FakeQuantizationMixin.wrap(nn.AdaptiveMaxPool2d)
FakeQuantizedAdaptiveMaxPool3d = FakeQuantizationMixin.wrap(nn.AdaptiveMaxPool3d)
FakeQuantizedAlphaDropout = FakeQuantizationMixin.wrap(nn.AlphaDropout)
FakeQuantizedAvgPool1d = FakeQuantizationMixin.wrap(nn.AvgPool1d)
FakeQuantizedAvgPool2d = FakeQuantizationMixin.wrap(nn.AvgPool2d)
FakeQuantizedAvgPool3d = FakeQuantizationMixin.wrap(nn.AvgPool3d)
# FakeQuantizedBCELoss = FakeQuantizationMixin.wrap(nn.BCELoss)
# FakeQuantizedBCEWithLogitsLoss = FakeQuantizationMixin.wrap(nn.BCEWithLogitsLoss)
FakeQuantizedBatchNorm1d = FakeQuantizationMixin.wrap(nn.BatchNorm1d)
FakeQuantizedBatchNorm2d = FakeQuantizationMixin.wrap(nn.BatchNorm2d)
FakeQuantizedBatchNorm3d = FakeQuantizationMixin.wrap(nn.BatchNorm3d)
# FakeQuantizedBilinear = FakeQuantizationMixin.wrap(nn.Bilinear)
FakeQuantizedCELU = FakeQuantizationMixin.wrap(nn.CELU)
# FakeQuantizedCTCLoss = FakeQuantizationMixin.wrap(nn.CTCLoss)
FakeQuantizedChannelShuffle = FakeQuantizationMixin.wrap(nn.ChannelShuffle)
FakeQuantizedConstantPad1d = FakeQuantizationMixin.wrap(nn.ConstantPad1d)
FakeQuantizedConstantPad2d = FakeQuantizationMixin.wrap(nn.ConstantPad2d)
FakeQuantizedConstantPad3d = FakeQuantizationMixin.wrap(nn.ConstantPad3d)
FakeQuantizedConv1d = FakeQuantizationMixin.wrap(nn.Conv1d)
FakeQuantizedConv2d = FakeQuantizationMixin.wrap(nn.Conv2d)
FakeQuantizedConv3d = FakeQuantizationMixin.wrap(nn.Conv3d)
FakeQuantizedConvTranspose1d = FakeQuantizationMixin.wrap(nn.ConvTranspose1d)
FakeQuantizedConvTranspose2d = FakeQuantizationMixin.wrap(nn.ConvTranspose2d)
FakeQuantizedConvTranspose3d = FakeQuantizationMixin.wrap(nn.ConvTranspose3d)
# FakeQuantizedCosineEmbeddingLoss = FakeQuantizationMixin.wrap(nn.CosineEmbeddingLoss)
# FakeQuantizedCosineSimilarity = FakeQuantizationMixin.wrap(nn.CosineSimilarity)
# FakeQuantizedCrossEntropyLoss = FakeQuantizationMixin.wrap(nn.CrossEntropyLoss)
# FakeQuantizedCrossMapLRN2d = FakeQuantizationMixin.wrap(nn.CrossMapLRN2d)
FakeQuantizedDropout = FakeQuantizationMixin.wrap(nn.Dropout)
# FakeQuantizedDropout1d = FakeQuantizationMixin.wrap(nn.Dropout1d) # Not supported in torch-1.9
FakeQuantizedDropout2d = FakeQuantizationMixin.wrap(nn.Dropout2d)
FakeQuantizedDropout3d = FakeQuantizationMixin.wrap(nn.Dropout3d)
FakeQuantizedELU = FakeQuantizationMixin.wrap(nn.ELU)
FakeQuantizedEmbedding = FakeQuantizationMixin.wrap(nn.Embedding)
# FakeQuantizedEmbeddingBag = FakeQuantizationMixin.wrap(nn.EmbeddingBag)
FakeQuantizedFeatureAlphaDropout = FakeQuantizationMixin.wrap(nn.FeatureAlphaDropout)
FakeQuantizedFlatten = FakeQuantizationMixin.wrap(nn.Flatten)
FakeQuantizedFold = FakeQuantizationMixin.wrap(nn.Fold)
FakeQuantizedFractionalMaxPool2d = FakeQuantizationMixin.wrap(nn.FractionalMaxPool2d)
FakeQuantizedFractionalMaxPool3d = FakeQuantizationMixin.wrap(nn.FractionalMaxPool3d)
FakeQuantizedGELU = FakeQuantizationMixin.wrap(nn.GELU)
FakeQuantizedGLU = FakeQuantizationMixin.wrap(nn.GLU)
# FakeQuantizedGRU = FakeQuantizationMixin.wrap(nn.GRU)
# FakeQuantizedGRUCell = FakeQuantizationMixin.wrap(nn.GRUCell)
# FakeQuantizedGaussianNLLLoss = FakeQuantizationMixin.wrap(nn.GaussianNLLLoss)
FakeQuantizedGroupNorm = FakeQuantizationMixin.wrap(nn.GroupNorm)
FakeQuantizedHardshrink = FakeQuantizationMixin.wrap(nn.Hardshrink)
FakeQuantizedHardsigmoid = FakeQuantizationMixin.wrap(nn.Hardsigmoid)
FakeQuantizedHardswish = FakeQuantizationMixin.wrap(nn.Hardswish)
FakeQuantizedHardtanh = FakeQuantizationMixin.wrap(nn.Hardtanh)
# FakeQuantizedHingeEmbeddingLoss = FakeQuantizationMixin.wrap(nn.HingeEmbeddingLoss)
# FakeQuantizedHuberLoss = FakeQuantizationMixin.wrap(nn.HuberLoss)
FakeQuantizedIdentity = FakeQuantizationMixin.wrap(nn.Identity)
FakeQuantizedInstanceNorm1d = FakeQuantizationMixin.wrap(nn.InstanceNorm1d)
FakeQuantizedInstanceNorm2d = FakeQuantizationMixin.wrap(nn.InstanceNorm2d)
FakeQuantizedInstanceNorm3d = FakeQuantizationMixin.wrap(nn.InstanceNorm3d)
# FakeQuantizedKLDivLoss = FakeQuantizationMixin.wrap(nn.KLDivLoss)
# FakeQuantizedL1Loss = FakeQuantizationMixin.wrap(nn.L1Loss)
FakeQuantizedLPPool1d = FakeQuantizationMixin.wrap(nn.LPPool1d)
FakeQuantizedLPPool2d = FakeQuantizationMixin.wrap(nn.LPPool2d)
# FakeQuantizedLSTM = FakeQuantizationMixin.wrap(nn.LSTM)
# FakeQuantizedLSTMCell = FakeQuantizationMixin.wrap(nn.LSTMCell)
FakeQuantizedLayerNorm = FakeQuantizationMixin.wrap(nn.LayerNorm)
# FakeQuantizedLazyBatchNorm1d = FakeQuantizationMixin.wrap(nn.LazyBatchNorm1d)
# FakeQuantizedLazyBatchNorm2d = FakeQuantizationMixin.wrap(nn.LazyBatchNorm2d)
# FakeQuantizedLazyBatchNorm3d = FakeQuantizationMixin.wrap(nn.LazyBatchNorm3d)
# FakeQuantizedLazyConv1d = FakeQuantizationMixin.wrap(nn.LazyConv1d)
# FakeQuantizedLazyConv2d = FakeQuantizationMixin.wrap(nn.LazyConv2d)
# FakeQuantizedLazyConv3d = FakeQuantizationMixin.wrap(nn.LazyConv3d)
# FakeQuantizedLazyConvTranspose1d = FakeQuantizationMixin.wrap(nn.LazyConvTranspose1d)
# FakeQuantizedLazyConvTranspose2d = FakeQuantizationMixin.wrap(nn.LazyConvTranspose2d)
# FakeQuantizedLazyConvTranspose3d = FakeQuantizationMixin.wrap(nn.LazyConvTranspose3d)
# FakeQuantizedLazyInstanceNorm1d = FakeQuantizationMixin.wrap(nn.LazyInstanceNorm1d)
# FakeQuantizedLazyInstanceNorm2d = FakeQuantizationMixin.wrap(nn.LazyInstanceNorm2d)
# FakeQuantizedLazyInstanceNorm3d = FakeQuantizationMixin.wrap(nn.LazyInstanceNorm3d)
# FakeQuantizedLazyLinear = FakeQuantizationMixin.wrap(nn.LazyLinear)
FakeQuantizedLeakyReLU = FakeQuantizationMixin.wrap(nn.LeakyReLU)
FakeQuantizedLinear = FakeQuantizationMixin.wrap(nn.Linear)
FakeQuantizedLocalResponseNorm = FakeQuantizationMixin.wrap(nn.LocalResponseNorm)
FakeQuantizedLogSigmoid = FakeQuantizationMixin.wrap(nn.LogSigmoid)
FakeQuantizedLogSoftmax = FakeQuantizationMixin.wrap(nn.LogSoftmax)
# FakeQuantizedMSELoss = FakeQuantizationMixin.wrap(nn.MSELoss)
# FakeQuantizedMarginRankingLoss = FakeQuantizationMixin.wrap(nn.MarginRankingLoss)
FakeQuantizedMaxPool1d = FakeQuantizationMixin.wrap(nn.MaxPool1d)
FakeQuantizedMaxPool2d = FakeQuantizationMixin.wrap(nn.MaxPool2d)
FakeQuantizedMaxPool3d = FakeQuantizationMixin.wrap(nn.MaxPool3d)
# FakeQuantizedMaxUnpool1d = FakeQuantizationMixin.wrap(nn.MaxUnpool1d)
# FakeQuantizedMaxUnpool2d = FakeQuantizationMixin.wrap(nn.MaxUnpool2d)
# FakeQuantizedMaxUnpool3d = FakeQuantizationMixin.wrap(nn.MaxUnpool3d)
FakeQuantizedMish = FakeQuantizationMixin.wrap(nn.Mish)
FakeQuantizedModule = FakeQuantizationMixin.wrap(nn.Module)
# FakeQuantizedModuleDict = FakeQuantizationMixin.wrap(nn.ModuleDict)
# FakeQuantizedModuleList = FakeQuantizationMixin.wrap(nn.ModuleList)
# FakeQuantizedMultiLabelMarginLoss = FakeQuantizationMixin.wrap(nn.MultiLabelMarginLoss)
# FakeQuantizedMultiLabelSoftMarginLoss = FakeQuantizationMixin.wrap(nn.MultiLabelSoftMarginLoss)
# FakeQuantizedMultiMarginLoss = FakeQuantizationMixin.wrap(nn.MultiMarginLoss)
# FakeQuantizedMultiheadAttention = FakeQuantizationMixin.wrap(nn.MultiheadAttention)
# FakeQuantizedNLLLoss = FakeQuantizationMixin.wrap(nn.NLLLoss)
# FakeQuantizedNLLLoss2d = FakeQuantizationMixin.wrap(nn.NLLLoss2d)
FakeQuantizedPReLU = FakeQuantizationMixin.wrap(nn.PReLU)
# FakeQuantizedPairwiseDistance = FakeQuantizationMixin.wrap(nn.PairwiseDistance)
# FakeQuantizedParameterDict = FakeQuantizationMixin.wrap(nn.ParameterDict)
# FakeQuantizedParameterList = FakeQuantizationMixin.wrap(nn.ParameterList)
FakeQuantizedPixelShuffle = FakeQuantizationMixin.wrap(nn.PixelShuffle)
FakeQuantizedPixelUnshuffle = FakeQuantizationMixin.wrap(nn.PixelUnshuffle)
# FakeQuantizedPoissonNLLLoss = FakeQuantizationMixin.wrap(nn.PoissonNLLLoss)
# FakeQuantizedRNN = FakeQuantizationMixin.wrap(nn.RNN)
# FakeQuantizedRNNBase = FakeQuantizationMixin.wrap(nn.RNNBase)
# FakeQuantizedRNNCell = FakeQuantizationMixin.wrap(nn.RNNCell)
# FakeQuantizedRNNCellBase = FakeQuantizationMixin.wrap(nn.RNNCellBase)
FakeQuantizedRReLU = FakeQuantizationMixin.wrap(nn.RReLU)
FakeQuantizedReLU = FakeQuantizationMixin.wrap(nn.ReLU)
FakeQuantizedReLU6 = FakeQuantizationMixin.wrap(nn.ReLU6)
FakeQuantizedReflectionPad1d = FakeQuantizationMixin.wrap(nn.ReflectionPad1d)
FakeQuantizedReflectionPad2d = FakeQuantizationMixin.wrap(nn.ReflectionPad2d)
# FakeQuantizedReflectionPad3d = FakeQuantizationMixin.wrap(nn.ReflectionPad3d) # Not supported in torch-1.9
FakeQuantizedReplicationPad1d = FakeQuantizationMixin.wrap(nn.ReplicationPad1d)
FakeQuantizedReplicationPad2d = FakeQuantizationMixin.wrap(nn.ReplicationPad2d)
FakeQuantizedReplicationPad3d = FakeQuantizationMixin.wrap(nn.ReplicationPad3d)
FakeQuantizedSELU = FakeQuantizationMixin.wrap(nn.SELU)
FakeQuantizedSequential = FakeQuantizationMixin.wrap(nn.Sequential)
FakeQuantizedSiLU = FakeQuantizationMixin.wrap(nn.SiLU)
FakeQuantizedSigmoid = FakeQuantizationMixin.wrap(nn.Sigmoid)
# FakeQuantizedSmoothL1Loss = FakeQuantizationMixin.wrap(nn.SmoothL1Loss)
# FakeQuantizedSoftMarginLoss = FakeQuantizationMixin.wrap(nn.SoftMarginLoss)
FakeQuantizedSoftmax = FakeQuantizationMixin.wrap(nn.Softmax)
FakeQuantizedSoftmax2d = FakeQuantizationMixin.wrap(nn.Softmax2d)
FakeQuantizedSoftmin = FakeQuantizationMixin.wrap(nn.Softmin)
FakeQuantizedSoftplus = FakeQuantizationMixin.wrap(nn.Softplus)
FakeQuantizedSoftshrink = FakeQuantizationMixin.wrap(nn.Softshrink)
FakeQuantizedSoftsign = FakeQuantizationMixin.wrap(nn.Softsign)
FakeQuantizedSyncBatchNorm = FakeQuantizationMixin.wrap(nn.SyncBatchNorm)
FakeQuantizedTanh = FakeQuantizationMixin.wrap(nn.Tanh)
FakeQuantizedTanhshrink = FakeQuantizationMixin.wrap(nn.Tanhshrink)
FakeQuantizedThreshold = FakeQuantizationMixin.wrap(nn.Threshold)
# FakeQuantizedTransformer = FakeQuantizationMixin.wrap(nn.Transformer)
# FakeQuantizedTransformerDecoder = FakeQuantizationMixin.wrap(nn.TransformerDecoder)
# FakeQuantizedTransformerDecoderLayer = FakeQuantizationMixin.wrap(nn.TransformerDecoderLayer)
# FakeQuantizedTransformerEncoder = FakeQuantizationMixin.wrap(nn.TransformerEncoder)
# FakeQuantizedTransformerEncoderLayer = FakeQuantizationMixin.wrap(nn.TransformerEncoderLayer)
# FakeQuantizedTripletMarginLoss = FakeQuantizationMixin.wrap(nn.TripletMarginLoss)
# FakeQuantizedTripletMarginWithDistanceLoss = FakeQuantizationMixin.wrap(nn.TripletMarginWithDistanceLoss)
FakeQuantizedUnflatten = FakeQuantizationMixin.wrap(nn.Unflatten)
FakeQuantizedUnfold = FakeQuantizationMixin.wrap(nn.Unfold)
FakeQuantizedUpsample = FakeQuantizationMixin.wrap(nn.Upsample)
FakeQuantizedUpsamplingBilinear2d = FakeQuantizationMixin.wrap(nn.UpsamplingBilinear2d)
FakeQuantizedUpsamplingNearest2d = FakeQuantizationMixin.wrap(nn.UpsamplingNearest2d)
FakeQuantizedZeroPad2d = FakeQuantizationMixin.wrap(nn.ZeroPad2d)
