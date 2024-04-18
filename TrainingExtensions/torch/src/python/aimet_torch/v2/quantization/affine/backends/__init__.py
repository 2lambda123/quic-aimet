# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
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
# pylint: disable=all

import math
from typing import overload, Union, Tuple, Optional, List
import torch
from .utils import *


@overload
def quantize(tensor: torch.Tensor, scale: torch.Tensor, offset: torch.Tensor,
             bitwidth: Union[int, float], signed: bool = False, block_size: Optional[List] = None):
    ...

@overload
def quantize(tensor: torch.Tensor, scale: torch.Tensor, offset: torch.Tensor, *,
             num_bins: int, signed: bool = False, block_size: Optional[List] = None):
    ...

@overload
def quantize(tensor: torch.Tensor, scale: torch.Tensor, offset: torch.Tensor, *,
             qmin: int, qmax: int, block_size: Optional[List] = None):
    ...


def quantize(tensor: torch.Tensor, scale: torch.Tensor, offset: torch.Tensor,
             *args, **kwargs):
    r"""
    Applies quantization to the input.

    Precisely,

    .. math::
        out = clamp\left(\left\lceil\frac{input}{scale}\right\rfloor - offset, qmin, qmax\right)


    This function is overloaded with the signatures listed below:


    .. function:: quantize(tensor, scale, offset, bitwidth, signed=False, block_size=None)
       :noindex:

       Equivalent to:

       .. math::
           qmin= 
           \begin{cases}
               -\left\lceil\frac{2^{bitwidth}-1}{2}\right\rceil,& \text{if } signed\\
               0,                                          & \text{otherwise   (default)}
           \end{cases}
           qmax= 
           \begin{cases}
               \left\lfloor\frac{2^{bitwidth}-1}{2}\right\rfloor,& \text{if } signed\\
               2^{bitwidth}-1,                                   & \text{otherwise   (default)}
           \end{cases}

       :param Tensor tensor: Tensor to quantize
       :param Tensor scale: Scale for quantization
       :param Tensor offset: Offset for quantization
       :param int bitwidth: Bitwidth of quantized tensor based on which :math:`qmin` and :math:`qmax` will be derived
       :param bool signed: If false, the output will be mapped to positive integers only.
           Otherwise, it will range over both positive and negative integers.

    .. function:: quantize(tensor, scale, offset, *, num_bins, signed=False, block_size=None)
       :noindex:

       Equivalent to:

       .. math::
           qmin= 
           \begin{cases}
               -\left\lceil\frac{num\_bins}{2}\right\rceil,& \text{if } signed\\
               0,                                          & \text{otherwise   (default)}
           \end{cases}
           qmax= 
           \begin{cases}
               \left\lfloor\frac{num\_bins}{2}\right\rfloor,& \text{if } signed\\
               num\_bins,                                   & \text{otherwise   (default)}
           \end{cases}


       :param Tensor tensor: Tensor to quantize
       :param Tensor scale: Scale for quantization
       :param Tensor offset: Offset for quantization
       :param int num_bins: The number of bins in the quantization range based on which :math:`qmin` and :math:`qmax` will be derived
       :param bool signed: If false, the output will be mapped to positive integers only.
           Otherwise, it will range over both positive and negative integers.

    .. function:: quantize(tensor, scale, offset, *, qmin, qmax, block_size=None)
       :noindex:

       :param Tensor tensor: Tensor to quantize
       :param Tensor scale: Scale for quantization
       :param Tensor offset: Offset for quantization
       :param int qmin: Minimum value of the quantization range
       :param int qmax: Maximum value of the quantization range
    """
    qmin, qmax, block_size = _parse_args(args, kwargs)
    return get_backend().quantize(tensor, scale, offset, qmin, qmax, block_size)


@overload
def quantize_dequantize(tensor: torch.Tensor, scale: torch.Tensor, offset: torch.Tensor,
                        bitwidth: Union[int, float], signed: bool = False, block_size: Optional[List] = None):
    ...

@overload
def quantize_dequantize(tensor: torch.Tensor, scale: torch.Tensor, offset: torch.Tensor, *,
                        num_bins: int, signed: bool = False, block_size: Optional[List] = None):
    ...

@overload
def quantize_dequantize(tensor: torch.Tensor, scale: torch.Tensor, offset: torch.Tensor, *,
                        qmin: int, qmax: int, block_size: Optional[List] = None):
    ...


def quantize_dequantize(tensor: torch.Tensor, scale: torch.Tensor, offset: torch.Tensor,
                        *args, **kwargs):
    r"""
    Applies fake-quantization by quantizing and dequantizing the input.

    Precisely,

    .. math::
        out = (x_{int} + offset) * scale

    where

    .. math::
        x_{int} = clamp\left(\left\lceil\frac{input}{scale}\right\rfloor - offset, qmin, qmax\right)


    This function is overloaded with the signatures listed below:


    .. function:: quantize_dequantize(tensor, scale, offset, bitwidth, signed=False, block_size=None)
       :noindex:

       Equivalent to:

       .. math::
           qmin= 
           \begin{cases}
               -\left\lceil\frac{2^{bitwidth}-1}{2}\right\rceil,& \text{if } signed\\
               0,                                          & \text{otherwise   (default)}
           \end{cases}
           qmax= 
           \begin{cases}
               \left\lfloor\frac{2^{bitwidth}-1}{2}\right\rfloor,& \text{if } signed\\
               2^{bitwidth}-1,                                   & \text{otherwise   (default)}
           \end{cases}

       :param Tensor tensor: Tensor to quantize
       :param Tensor scale: Scale for quantization
       :param Tensor offset: Offset for quantization
       :param int bitwidth: Bitwidth of quantized tensor based on which :math:`qmin` and :math:`qmax` will be derived
       :param bool signed: If false, the intermediate output :math:`x_{int}` will be mapped to positive integers only.
           Otherwise, :math:`x_{int}` will range over both positive and negative integers.

    .. function:: quantize_dequantize(tensor, scale, offset, *, num_bins, signed=False, block_size=None)
       :noindex:

       Equivalent to:

       .. math::
           qmin= 
           \begin{cases}
               -\left\lceil\frac{num\_bins}{2}\right\rceil,& \text{if } signed\\
               0,                                          & \text{otherwise   (default)}
           \end{cases}
           qmax= 
           \begin{cases}
               \left\lfloor\frac{num\_bins}{2}\right\rfloor,& \text{if } signed\\
               num\_bins,                                   & \text{otherwise   (default)}
           \end{cases}


       :param Tensor tensor: Tensor to quantize
       :param Tensor scale: Scale for quantization
       :param Tensor offset: Offset for quantization
       :param int num_bins: The number of bins in the quantization range based on which :math:`qmin` and :math:`qmax` will be derived
       :param bool signed: If false, the intermediate output :math:`x_{int}` will be mapped to positive integers only.
           Otherwise, :math:`x_{int}` will range over both positive and negative integers.

    .. function:: quantize_dequantize(tensor, scale, offset, *, qmin, qmax, block_size=None)
       :noindex:

       :param Tensor tensor: Tensor to quantize
       :param Tensor scale: Scale for quantization
       :param Tensor offset: Offset for quantization
       :param int qmin: Minimum value of the quantization range
       :param int qmax: Maximum value of the quantization range
    """
    qmin, qmax, block_size = _parse_args(args, kwargs)
    return get_backend().quantize_dequantize(tensor, scale, offset, qmin, qmax, block_size)


def dequantize(tensor: torch.Tensor, scale: torch.Tensor, offset: torch.Tensor, block_size: Optional[List] = None):
    return get_backend().dequantize(tensor, scale, offset, block_size)


def _parse_args(args, kwargs) -> Tuple[int, int, Optional[List]]:
    bitwidth = num_bins = signed = qmin = qmax = None
    block_size = kwargs.get('block_size')

    if len(args) == 2:
        bitwidth, signed = args
    elif len(args) == 1:
        bitwidth = args[0]
        signed = kwargs.get('signed', False)
    else:
        if 'bitwidth' in kwargs:
            bitwidth, signed = kwargs['bitwidth'], kwargs.get('signed', False)
        elif 'num_bins' in kwargs:
            num_bins, signed = kwargs['num_bins'], kwargs.get('signed', False)
        else:
            qmin, qmax = kwargs['qmin'], kwargs['qmax']

    if bitwidth is not None:
        num_bins = 2 ** bitwidth - 1

    if num_bins is not None:
        if signed:
            qmin = -math.ceil(num_bins/2)
            qmax = math.floor(num_bins/2)
        else:
            qmin = 0
            qmax = num_bins

    assert qmin is not None
    assert qmax is not None

    return qmin, qmax, block_size
