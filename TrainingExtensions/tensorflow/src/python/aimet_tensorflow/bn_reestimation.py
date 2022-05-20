# /usr/bin/env python3.6
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2022, Qualcomm Innovation Center, Inc. All rights reserved.
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

"""BatchNorm Reestimation"""
import fnmatch
from typing import List, Tuple, Dict
import numpy as np
import tensorflow as tf
from aimet_common.utils import AimetLogger
from aimet_tensorflow.common.graph_eval import initialize_uninitialized_vars
from aimet_tensorflow.utils.common import create_input_feed_dict, iterate_tf_dataset

logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.Quant)

class _Handle:
    """ Removable handle. """

    def __init__(self, cleanup_fn):
        self._cleanup_fn = cleanup_fn
        self._removed = False

    def remove(self):
        """ Run clean up function """
        if not self._removed:
            self._cleanup_fn()
            self._removed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.remove()


# pylint: disable=not-an-iterable
def _get_all_tf_bn_vars_list(sess: tf.compat.v1.Session, momentum_names: List[str],
                             training_names: List[str]) -> Tuple:
    """
    find tf varaible list to access
    :param sess: tf session
    :param momentum_names: BN's momentum name list
    :param training_names: BN's training_names list
    :return: tf.variable lists to access bn layers's mean,var,momentum,training
    """
    with sess.graph.as_default():
        mean_var_tf_var_list = []
        momentum_tf_var_list = []
        training_tf_var_list = []
        tf_global_vars = tf.compat.v1.get_collection(tf.compat.v1.GraphKeys.GLOBAL_VARIABLES)
        for v in tf_global_vars:
            if (fnmatch.fnmatch(v.name, "batch_normalization*moving_mean:0") or fnmatch.fnmatch(
                    v.name,
                    "batch_normalization*moving_variance:0")):
                mean_var_tf_var_list.append(v)
            for momentum_name in momentum_names:
                tf_var_name = momentum_name + ":0"
                if fnmatch.fnmatch(v.name, tf_var_name):
                    momentum_tf_var_list.append(v)
            for training_name in training_names:
                tf_var_name = training_name + ":0"
                if fnmatch.fnmatch(v.name, tf_var_name):
                    training_tf_var_list.append(v)

    return mean_var_tf_var_list, momentum_tf_var_list, training_tf_var_list

# pylint: disable=not-an-iterable
# pylint: disable=unsubscriptable-object
def _reset_bn_stats(sess: tf.compat.v1.Session, bn_mean_var_checkpoints: Dict, bn_momentum_checkpoints: Dict,
                    bn_training_checkpoints: Dict) -> _Handle:
    """
    reset bn stats
    :param sess: tf session
    :param bn_mean_var_checkpoints: Dict for original mean&var
    :param bn_momentum_checkpoints: Dict for original bn momentum
    :param bn_training_checkpoints: Dict for original bn training
    :return:
    """

    def cleanup():
        """
        Restore Bn stats
        """
        with sess.graph.as_default():
            for k in bn_mean_var_checkpoints.keys():
                sess.run(tf.compat.v1.assign(k, bn_mean_var_checkpoints[k]))
            for k in bn_momentum_checkpoints.keys():
                sess.run(tf.compat.v1.assign(k, bn_momentum_checkpoints[k]))
            for k in bn_training_checkpoints.keys():
                sess.run(tf.compat.v1.assign(k, bn_training_checkpoints[k]))

    try:
        with sess.graph.as_default():
            for k in bn_momentum_checkpoints.keys():
                sess.run(tf.compat.v1.assign(k, 0.0))
            for k in bn_training_checkpoints.keys():
                sess.run(tf.compat.v1.assign(k, tf.compat.v1.constant(True)))
        return _Handle(cleanup)
    except:
        cleanup()
        raise


# pylint: disable=too-many-arguments
# pylint: disable=not-an-iterable
# pylint: disable=unsubscriptable-object
# pylint: disable=too-many-locals
def reestimate_bn_stats(sess: tf.compat.v1.Session, start_op_names: List[str],
                        output_op_names: List[str], bn_momentum_names: List[str], bn_training_names: List[str],
                        bn_re_estimation_dataset: tf.compat.v1.data.Dataset, bn_num_batches: int = 100):
    """
    top level api for end user directly call for eval()
    :param sess_sim: tf quantized model
    :param start_op_names: List of starting op names of the model
    :param output_op_names: List of output op names of the model
    :param bn_momentum_names: BN's momentum name list
    :param bn_training_names: BN's training_names list
    :param bn_re_estimation_dataset: full or parts of Training dataset
    :param bn_num_batches: The number of batches to be used for reestimation
    """
    # setup tf varaible list to access
    bn_mean_var_tf_var_list, bn_momentum_tf_var_list, bn_training_tf_var_list = \
        _get_all_tf_bn_vars_list(sess, bn_momentum_names, bn_training_names)

    with sess.graph.as_default():
        # save checkpoints
        bn_momentum_checkpoints = {v: sess.run(v) for v in bn_momentum_tf_var_list}
        bn_training_checkpoints = {v: sess.run(v) for v in bn_training_tf_var_list}
        bn_mean_var_checkpoints = {v: sess.run(v) for v in bn_mean_var_tf_var_list}

    # 1. switch to re-estimation mode and setup remove
    handle = _reset_bn_stats(sess, bn_mean_var_checkpoints, bn_momentum_checkpoints, bn_training_checkpoints)
    # 2 per batch forward and BN re-estimation
    with sess.graph.as_default():
        output_ops = [sess.graph.get_operation_by_name(name) for name in output_op_names]
        output_tensors = [sess.graph.get_tensor_by_name(output_op.name + ':0') for output_op in output_ops]
        bn_dataset_iterator = iterate_tf_dataset(bn_re_estimation_dataset)
        update_ops = tf.compat.v1.get_collection(tf.compat.v1.GraphKeys.UPDATE_OPS)
        with tf.compat.v1.control_dependencies(update_ops):
            output_tensors = tf.compat.v1.identity(output_tensors)
        initialize_uninitialized_vars(sess)
        # (1)intilization
        sum_dict = {v: np.zeros(v.shape, dtype=v.dtype.as_numpy_dtype) for v in bn_mean_var_tf_var_list}
        # (2)forward and accumulate mean and var
    for batch_index in range(bn_num_batches):
        try:
            batch_data = next(bn_dataset_iterator)
            feed_dict = create_input_feed_dict(sess.graph, start_op_names, batch_data)
            sess.run(output_tensors, feed_dict=feed_dict)
            for v in bn_mean_var_tf_var_list:
                # sum_dict[v.name] += sess.run(v)
                sum_dict[v] += sess.run(v)
            if batch_index == bn_num_batches - 1:
                break
        except tf.errors.OutOfRangeError:
            logger.info("tf.errors.OutOfRangeError:: no data from BN dataset.")  # ==> "End of dataset"
            break
    # (3) average mean&var
    for k in sum_dict.keys():
        sum_dict[k] = sum_dict[k] / bn_num_batches
    # (4) apply result: a.update BN stats with new  b.restore momentum  c. restore training
    with sess.graph.as_default():
        for k in bn_mean_var_checkpoints.keys():
            sess.run(tf.compat.v1.assign(k, sum_dict[k]))
        for k in bn_momentum_checkpoints.keys():
            sess.run(tf.compat.v1.assign(k, bn_momentum_checkpoints[k]))
        for k in bn_training_checkpoints.keys():
            sess.run(tf.compat.v1.assign(k, bn_training_checkpoints[k]))

    return handle
