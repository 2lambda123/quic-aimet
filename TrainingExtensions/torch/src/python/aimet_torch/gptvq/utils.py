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
"""Utility methods for working with GPTVQ"""
from typing import Optional

import torch
from torch.linalg import LinAlgError

from aimet_torch.gptvq.defs import DAMPENING_PERCENTAGE


def generate_codebook(weight_block: torch.Tensor,
                      num_of_centroids: int,
                      inverse_hessian_diagonal: Optional[torch.Tensor] = None,
                      assignment_chunk_size: Optional[int] = None,
                      kmeans_iteration: int = 100):
    """
    Generate and optimize codebook using K-means and return it

    :param weight_block: Weight block
    :param num_of_centroids: Number of centroids
    :param inverse_hessian_diagonal: Diagonal of inverse Hessian tensor
    :param assignment_chunk_size: Chunk size for better memory management
    :param kmeans_iteration: Number of K-means iterations
    :return: Optimized codebook
    """
    codebook = hacky_mahalanobis_init(weight_block, num_of_centroids)
    for _ in range(kmeans_iteration):
        # Expectation step
        assignments = get_assignments(weight_block, codebook, inverse_hessian_diagonal, assignment_chunk_size)

        # Maximization step
        codebook = do_kmeans_maximization(weight_block, codebook, assignments, inverse_hessian_diagonal)
    return codebook


def hacky_mahalanobis_init(tensor: torch.Tensor, num_of_centroids: int) -> torch.Tensor:
    """
    Initialize centroids using hacky Mahalanobis

    :param tensor: num_blocks_per_column x N x vector_dim weight tensor
    :param num_of_centroids: Number of centroids
    :return: Initialized codebook
    """
    vector_dim = tensor.shape[-1]
    mu = tensor.mean(1).unsqueeze(1)
    x_centered = tensor - mu
    sigma = torch.bmm(x_centered.transpose(1, 2), x_centered)  # num_blocks_per_column x vector_dim x vector_dim

    diag = torch.arange(sigma.shape[-1], device=sigma.device)
    damp = DAMPENING_PERCENTAGE * torch.mean(sigma[:, diag, diag].abs(), dim=-1)
    sigma[:, diag, diag] += damp[..., None]

    try:
        lambda_ = torch.linalg.inv(sigma)
    except LinAlgError:
        lambda_ = torch.zeros_like(sigma)
        lambda_[:, diag, diag] = 1.0

    dists = (torch.bmm(x_centered, lambda_) * x_centered).sum(-1)  # num_blocks_per_column x N
    sorted_dists = torch.argsort(dists, dim=1)  # num_blocks_per_column x N
    idx = torch.round(torch.linspace(0, x_centered.shape[1] - 1, num_of_centroids)).long()  # num_of_centroids

    # num_blocks_per_column x num_of_centroids --> num_blocks_per_column x num_of_centroids x 1 --> num_blocks_per_column x num_of_centroids x vector_dim
    idx = (sorted_dists[:, idx].unsqueeze(-1).expand(-1, -1, vector_dim))
    return torch.gather(tensor, dim=1, index=idx)


def manipulate_inverse_hessian_diagonal(tensor: torch.Tensor,
                                        inverse_hessian_diagonal: Optional[torch.Tensor]) -> torch.Tensor:
    """
    Manipulate diagonal of inverse Hessian tensor if needed

    :param tensor: Tensor corresponding to diagonal of inverse Hessian tensor
    :param inverse_hessian_diagonal: Diagonal of inverse Hessian tensor
    :return: Manipulated Hessian tensor
    """
    if inverse_hessian_diagonal is None:
        return torch.ones(tensor.shape[-1], device=tensor.device)

    if inverse_hessian_diagonal.ndim > 2:  # should then be 1 x N x vector_dim
        assert (
                inverse_hessian_diagonal.shape[0] == 1
                and inverse_hessian_diagonal.shape[1] == tensor.shape[1]
                and inverse_hessian_diagonal.shape[2] == tensor.shape[2]
        ), f"{inverse_hessian_diagonal.shape, tensor.shape}"
        return inverse_hessian_diagonal.unsqueeze(2)  # 1 x N x 1 x vector_dim

    return inverse_hessian_diagonal


def generate_tensor_chunks(tensor: torch.Tensor,
                           chunk_size: Optional[int]) -> list[torch.Tensor]:
    """
    Generate chunks of torch.Tensor

    :param tensor: torch.Tensor
    :param chunk_size: Chunk size
    :return: Tensor chunks
    """
    if chunk_size is None:
        return [tensor]

    return torch.split(tensor, chunk_size, dim=1)


def generate_hessian_chunks(hessian: torch.Tensor,
                            num_of_tensor_chunks: int,
                            chunk_size: Optional[int]) -> list[torch.Tensor]:
    """
    Generate chunks of diagonal of inverse Hessian tensor

    :param hessian: Diagonal of inverse Hessian tensor
    :param num_of_tensor_chunks: Number of corresponding tensor chunks
    :param chunk_size: Chunk size
    :return: Hessian tensor chunks
    """
    if chunk_size is None:
        return [hessian]

    if hessian.ndim > 1:
        return torch.split(hessian, chunk_size, dim=1)

    return [hessian] * num_of_tensor_chunks


def prepare_tensor_and_hessian_chunks(tensor: torch.Tensor,
                                      hessian: torch.Tensor,
                                      chunk_size: Optional[int]) -> tuple[list[torch.Tensor], list[torch.Tensor]]:
    """
    Use chunking for better memory management and return tensor and hessian chunks

    :param tensor: Tensor corresponding to diagonal of inverse Hessian tensor
    :param hessian: Diagonal of inverse Hessian tensor
    :param chunk_size: Chunk size
    :return: Tuple of tensor chunks and hessian chunks
    """
    tensor_chunks = generate_tensor_chunks(tensor, chunk_size)
    hessian_chunks = generate_hessian_chunks(hessian, len(tensor_chunks), chunk_size)

    return tensor_chunks, hessian_chunks


def get_assignments(tensor: torch.Tensor,
                    centroids: torch.Tensor,
                    inverse_hessian_diagonal: Optional[torch.Tensor] = None,
                    chunk_size: Optional[int] = None) -> torch.Tensor:
    """
    Calculate nearest centroid index tensor

    :param tensor: num_blocks_per_column x N x vector_dim
    :param centroids: num_blocks_per_column x num_centroids x vector_dim
    :param inverse_hessian_diagonal: Diagonal of inverse Hessian tensor
    :param chunk_size: Chunk size for better memory management
    :return: nearest centroid index tensor
    """
    manipulated_hessian = manipulate_inverse_hessian_diagonal(tensor, inverse_hessian_diagonal)
    tensor_chunks, hessian_chunks = prepare_tensor_and_hessian_chunks(tensor, manipulated_hessian, chunk_size)

    centroids = centroids.unsqueeze(1) # num_blocks_per_column x 1 x num_centroids x vector_dim
    assignments = []
    for tensor_chunk, hessian_chunk in zip(tensor_chunks, hessian_chunks):
        tensor_chunk = tensor_chunk.unsqueeze(2) # num_blocks_per_column x N x 1 x vector_dim
        distance = ((tensor_chunk - centroids).pow(2) * hessian_chunk).sum(-1)
        assignments.append(distance.argmin(-1))

    return torch.concat(assignments, dim=1)  # num_blocks_per_column x N


def do_kmeans_maximization(tensor: torch.Tensor,
                           centroids: torch.Tensor,
                           assignments: torch.Tensor,
                           inverse_hessian_diagonal: Optional[torch.Tensor]) -> torch.Tensor:
    """
    Do K-means maximization step

    :param tensor: torch.Tensor (num_blocks_per_column x N x vector_dim)
    :param centroids: Codebook including centroids (num_blocks_per_column x num_centroids x vector_dim)
    :param assignments: Assignment result from expectation step (num_blocks_per_column x N)
    :param inverse_hessian_diagonal: Diagonal of inverse Hessian (1 x N x vector_dim)
    :return: Updated codebook after maximization step
    """
    centroid_range = torch.arange(centroids.shape[1], device=centroids.device)
    expanded_assignments = (
        assignments.unsqueeze(-1) == centroid_range.view(1, 1, -1)
    ).to(tensor.dtype)

    if inverse_hessian_diagonal is None:
        norm = 1.0 / torch.clip(expanded_assignments.sum(1), min=1)
        new_centroids = torch.einsum(
            "gnd,gnk,gk->gkd", tensor, expanded_assignments, norm
        )
    else:
        norm = 1.0 / torch.clip(
            torch.einsum(
                "gnk,nd->gkd", expanded_assignments, inverse_hessian_diagonal[0]
            ),
            min=1e-10,
        )
        new_centroids = torch.einsum(
            "gnd,nd,gnk,gkd->gkd",
            tensor,
            inverse_hessian_diagonal[0],
            expanded_assignments,
            norm,
        )

    return new_centroids
