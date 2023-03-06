//==============================================================================
//
//  @@-COPYRIGHT-START-@@
//
//  Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
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

#ifndef AIMET_MAIN_QCQUANTIZEOP_H
#define AIMET_MAIN_QCQUANTIZEOP_H

#define ORT_API_MANUAL_INIT
#include "onnxruntime_cxx_api.h"
#undef ORT_API_MANUAL_INIT

#include "QcQuantizeInfo.h"
#include <DlQuantization/TensorQuantizerOpFacade.h>

#ifdef ONNX_CUDA
#include <cuda.h>
#include <cuda_runtime.h>
#include<cuda_runtime_api.h>
#endif


struct QcQuantizeKernel
{
public:
    QcQuantizeKernel(const OrtApi* api, const OrtKernelInfo* info, bool useCuda);

    void Compute(OrtKernelContext* context);

private:
    const OrtKernelInfo* info_;
    Ort::CustomOpApi api_;
    struct QcQuantizeInfo* quant_info;
    bool useCuda;
};


struct QcQuantizeOp : Ort::CustomOpBase<QcQuantizeOp, QcQuantizeKernel>
{
    static void* CreateKernel(const OrtApi& api, const OrtKernelInfo* info);
    static const char* GetName();
    static size_t GetInputTypeCount();
    static ONNXTensorElementDataType GetInputType(size_t index);
    static size_t GetOutputTypeCount();
    static ONNXTensorElementDataType GetOutputType(size_t index);
    const char* GetExecutionProviderType() const;
};


#ifdef ONNX_CUDA
struct QcQuantizeOpGPU : Ort::CustomOpBase<QcQuantizeOpGPU, QcQuantizeKernel>
{
    static void* CreateKernel(const OrtApi& api, const OrtKernelInfo* info);
    static const char* GetName();
    static size_t GetInputTypeCount();
    static ONNXTensorElementDataType GetInputType(size_t index);
    static size_t GetOutputTypeCount();
    static ONNXTensorElementDataType GetOutputType(size_t index);
    const char* GetExecutionProviderType() const;
};
#endif


extern "C" ORT_EXPORT OrtStatus* ORT_API_CALL RegisterCustomOps(OrtSessionOptions* options, const OrtApiBase* api);


#endif   // AIMET_MAIN_QCQUANTIZEOP_H
