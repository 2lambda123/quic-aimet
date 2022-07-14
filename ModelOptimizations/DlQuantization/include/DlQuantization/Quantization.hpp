//==============================================================================
//
//  @@-COPYRIGHT-START-@@
//
//  Copyright (c) 2016-2022, Qualcomm Innovation Center, Inc. All rights reserved.
//
//  Redistribution and use in source and binary forms, with or without
//  modification, are permitted provided that the following conditions are met:
//
//  1. Redistributions of source code must retain the above copyright notice,
//     this list of conditions and the following disclaimer.
//
//  2. Redistributions in binary form must reproduce the above copyright notice,
//     this list of conditions and the following disclaimer in the documentation
//     and/or other materials provided with the distribution.
//
//  3. Neither the name of the copyright holder nor the names of its contributors
//     may be used to endorse or promote products derived from this software
//     without specific prior written permission.
//
//  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
//  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
//  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
//  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
//  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
//  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
//  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
//  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
//  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
//  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
//  POSSIBILITY OF SUCH DAMAGE.
//
//  SPDX-License-Identifier: BSD-3-Clause
//
//  @@-COPYRIGHT-END-@@
//
//==============================================================================


#ifndef QUANTIZATION_HPP
#define QUANTIZATION_HPP

#include <vector>

namespace DlQuantization
{
/**
 * Run on the CPU or GPU.
 */
enum ComputationMode
{
    COMP_MODE_CPU,
    COMP_MODE_GPU
};

/**
 * @brief The fixed point quantization mode defines what fixed point scheme
 * we use, and how we compute a suitable encoding.
 */
enum QuantizationMode
{
    // TensorFlow quantization with static encoding; zero can be represented.
    QUANTIZATION_TF,

    // An enhanced version of TensorFlow. The encoding is chosen such that the SQNR is maximal.
    QUANTIZATION_TF_ENHANCED,

    // Ranges (min, max) are learnt during training
    QUANTIZATION_RANGE_LEARNING,

    // Percentile calibration. Compute the encoding by adjusting the min/max range of the tensor
    // by clipping percentile of outliers.
    QUANTIZATION_PERCENTILE,

    // Compute the encoding by adjusting the min/max range of the tensor based on the mean square
    // error due to quantization of the tensor.
    QUANTIZATION_MSE,
};

/**
 * @brief TensorFlow-style fixed point format.
 *
 * We use this fixed point format for both TensorFlow quantization and Q-format
 * quantization.
 */
struct TfEncoding
{
    double min;
    double max;
    double delta;
    double offset;
    int bw;
};

/**
 * @brief The fixed point format of activations in a given network layer.
 */
struct TfEncodingLayer
{
    // The encoding of input tensors.
    std::vector<TfEncoding> in;
    // The encoding of output tensors.
    std::vector<TfEncoding> out;
};

/**
 * @brief Layer input or output activations.
 */
enum LayerInOut
{
    LAYER_INPUT,
    LAYER_OUTPUT
};

/**
 * @brief Rounding mode.
 */
enum RoundingMode
{
    ROUND_NEAREST,
    ROUND_STOCHASTIC
};

}   // End of namespace DlQuantization.

#endif   // QUANTIZATION_HPP
