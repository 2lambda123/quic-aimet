# /usr/bin/env python
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

import pytest
import numpy
import torch
from torch.utils.data import Dataset, DataLoader

from aimet_torch.quantsim import QuantizationSimModel
from aimet_torch.qc_quantize_op import StaticGridQuantWrapper, QuantScheme
from aimet_torch.seq_mse import  apply_seq_mse, get_candidates, optimize_module, SeqMseParams
from models.mnist_torch_model import Net

@pytest.fixture(scope="session")
def dummy_input():
    return torch.randn((1, 1, 28, 28))


@pytest.fixture(scope="session")
def unlabeled_data_loader(dummy_input):
    class MyDataset(Dataset):
        def __init__(self, data):
            self.data = data

        def __getitem__(self, index):
            return self.data[index]

        def __len__(self):
            return len(self.data)

    dataset = MyDataset([dummy_input[0, :] for _ in range(32)])
    return DataLoader(dataset)


class TestSeqMse:

    def test_seq_mse(self):
        """ test get_candidates() """
        torch.manual_seed(0)
        linear = torch.nn.Linear(2, 4)
        x_max = torch.max(linear.weight.abs(), dim=1)[0]
        x_min = None
        candidates = get_candidates(20, x_max, x_min)
        for cand_max, cand_min in candidates:
            assert list(cand_max.size())[0] == linear.out_features
            assert list(cand_min.size())[0] == linear.out_features

    @pytest.mark.parametrize("enable_pcq", [True, False])
    @pytest.mark.parametrize("param_bw", [2, 31])
    def test_optimize_module_linear(self, enable_pcq, param_bw):
        """ test optimize module for linear """
        torch.manual_seed(0)
        linear = torch.nn.Linear(64, 128)
        wrapper = StaticGridQuantWrapper(linear, param_bw, 16, 'nearest', QuantScheme.post_training_tf)
        wrapper.input_quantizers[0].enabled = False
        wrapper.output_quantizers[0].enabled = False
        if enable_pcq:
            wrapper.enable_per_channel_quantization()

        xq = torch.randn(32, 4, 32, 64)
        wrapper.param_quantizers['weight'].reset_encoding_stats()
        wrapper.param_quantizers['weight'].update_encoding_stats(wrapper.weight.data)
        wrapper.param_quantizers['weight'].compute_encoding()
        before = wrapper.param_quantizers['weight'].encoding
        params = SeqMseParams(num_batches=32)
        optimize_module(wrapper, xq, xq, params)
        after = wrapper.param_quantizers['weight'].encoding

        # If we use higher param_bw (for example 16, 31), then it should always choose larger candidates so
        # before and after param encodings should be almost same.
        if param_bw == 31:
            if enable_pcq:
                assert numpy.isclose(before[0].min, after[0].min)
                assert numpy.isclose(before[0].max, after[0].max)
            else:
                assert numpy.isclose(before.min, after.min)
                assert numpy.isclose(before.max, after.max)
        else:
            if enable_pcq:
                assert not numpy.isclose(before[0].min, after[0].min)
                assert not numpy.isclose(before[0].max, after[0].max)
            else:
                assert not numpy.isclose(before.min, after.min)
                assert not numpy.isclose(before.max, after.max)

    @pytest.mark.cuda()
    @pytest.mark.parametrize("inp_symmetry", ['asym', 'symfp', 'symqt'])
    @pytest.mark.parametrize("loss_fn", ['mse', 'l1', 'aa'])
    def test_apply_seq_mse(self, unlabeled_data_loader, inp_symmetry, loss_fn):
        """ test apply_seq_mse end-to-end """
        torch.manual_seed(0)
        model = Net().eval().cuda()
        dummy_input = torch.randn(1, 1, 28, 28).cuda()
        sim = QuantizationSimModel(model, dummy_input, default_param_bw=4, quant_scheme=QuantScheme.post_training_tf)
        params = SeqMseParams(num_batches=2, inp_symmetry=inp_symmetry, loss_fn=loss_fn)
        apply_seq_mse(model, sim, unlabeled_data_loader, params, modules_to_exclude=[sim.model.conv1])
        assert sim.model.fc1.param_quantizers['weight'].is_encoding_frozen
        assert sim.model.fc2.param_quantizers['weight'].is_encoding_frozen
        assert not sim.model.conv1.param_quantizers['weight'].encoding
        assert sim.model.conv2.param_quantizers['weight'].encoding