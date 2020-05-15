# /usr/bin/env python3.5
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2019, Qualcomm Innovation Center, Inc. All rights reserved.
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
""" utilities for fused batchnorm op """

from typing import Union
import tensorflow as tf
from tensorflow.contrib import graph_editor as ge
from aimet_common.utils import AimetLogger
from aimet_tensorflow.utils import constants

logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.Utils)

_BN_STRUCTURE_ERROR_MSG = "BN op doesn't have the expected structure"


class BNUtils:
    """ Batch Norm/ fused Batch Norm op related utils"""

    @staticmethod
    def skip_bn_op(sess: tf.Session, bn_op: tf.Operation, in_tensor: tf.Tensor, out_tensor: tf.Tensor):
        """
        skip given bn op specified (fused batch norm op)
        Note : supports only Fused bn op types.
        """

        if in_tensor is None or out_tensor is None:
            logger.error("Error, input and output tensors must be provided for skipping the op")
            assert False
        else:
            with sess.graph.as_default():
                if bn_op.type in ['FusedBatchNormV3']:
                    ge.detach_outputs(in_tensor.op)
                    ge.reroute_ts(in_tensor, out_tensor)
                    BNUtils.remove_bn_op_from_update_ops(sess, bn_op)
                else:
                    logger.error("Error, Unknown BN op")
                    assert False

    @staticmethod
    def _get_tensor_read_var_op_trainable_bn_op(input_tensor: tf.Tensor) -> tf.Tensor:
        """
         generic helper to find a read op tensor associated with input tensor
         that can be evaluated, when the bn op is marked trainable.
        :return: read var op type tensor as tf.Tensor type.
        """

        logger.debug('Fetching params from trainable BN op type')
        assert input_tensor.op.inputs[0].op.inputs is not None
        # inputs of 0 is beta tensor , get readVarOp associated with it
        var_tensor = input_tensor.op.inputs[0].op.inputs[0]
        assert var_tensor.op.outputs is not None
        assert len(var_tensor.consumers()) >= 3

        tensor_consumers = var_tensor.consumers()

        var_read_tensor = None
        # get read variable op tensor from these consumers
        # do not pick the one with _1 , it is not fetch-able
        for consumer in tensor_consumers:
            if consumer.type == 'ReadVariableOp' and 'ReadVariableOp_1' not in consumer.name:
                assert consumer.outputs is not None
                var_read_tensor = consumer.outputs[0]
                break

        assert var_read_tensor is not None

        return var_read_tensor

    @staticmethod
    def get_beta_read_op(bn_op: tf.Operation) -> tf.Operation:
        """
        get beta read op from BN op specified
        :param bn_op: bn_op obtained from connected graph using get_modules
         (is mul_1 op inside BN scope)
        :return: beta read op
        """
        if bn_op.type in ['Mul']:
            # For regular BN
            # mul_1 -> add_1 <-- sub <-- beta_read
            assert len(bn_op.outputs) >= 1, _BN_STRUCTURE_ERROR_MSG
            add_1 = bn_op.outputs[0].consumers()[0]
            assert len(add_1.inputs) >= 2, _BN_STRUCTURE_ERROR_MSG
            sub = add_1.inputs[1].op
            assert len(sub.inputs) >= 1, _BN_STRUCTURE_ERROR_MSG
            beta_read = sub.inputs[0].op
        elif bn_op.type in ['FusedBatchNormV3']:
            assert len(bn_op.inputs) == 5
            beta_read = bn_op.inputs[constants.BN_OP_PARAM_INDICES['beta']].op
            if beta_read.type == 'Switch':      # tf slim bn using training tensor form
                beta_read = beta_read.inputs[0].op
                assert 'read' in beta_read.name
        else:
            logger.error("Error, unknown BN op")
            assert False

        assert beta_read.type in ['ReadVariableOp', 'Identity']      # Will be identity for tf slim BNs
        return beta_read

    @staticmethod
    def get_beta_read_var_op_tensor(bn_op: tf.Operation) -> tf.Tensor:
        """
        get beta readVariableOp tensor from BN op specified
        :param bn_op: FusedBatchNorm as tf.Operation
        :return: tensor associated with bn op beta readVariableOp type, as tf.Tensor
        """
        assert bn_op.type in ['FusedBatchNormV3', 'Mul']
        beta_read_tensor = BNUtils.get_beta_read_op(bn_op).outputs[0]

        assert beta_read_tensor is not None

        if beta_read_tensor.op.inputs[0].op.type == 'Switch':
            logger.debug('Fetching params from trainable BN op type')
            beta_read_tensor = BNUtils._get_tensor_read_var_op_trainable_bn_op(beta_read_tensor)

        return beta_read_tensor

    @staticmethod
    def get_beta_as_numpy_data(sess: tf.Session, bn_op: tf.Operation):
        """
        get beta param from BN op specified
        :param sess: tensorflow session
        :param bn_op: bn_op as tf.Operation
        :return: beta tensor as numpy data
        """
        try:
            # try name based tensor look up for Keras layers
            beta_tensor = BNUtils._get_bn_param_tensor_using_name(sess, bn_op,
                                                                  constants.BNOpParamType.beta)
        except KeyError:
            # if we can't find the tensor name, use structure match
            # to figure out the read tensor for param
            beta_tensor = BNUtils.get_beta_read_var_op_tensor(bn_op)

        with sess.graph.as_default():
            numpy_data = sess.run(beta_tensor)
        return numpy_data

    @staticmethod
    def get_gamma_as_read_op(bn_op: tf.Operation):
        """
        get gamma read op from BN op specified
        :param bn_op: bn_op obtained from connected graph using get_modules
        (is mul_1 op inside BN scope)
        :return: gamma read op
        """
        if bn_op.type in ['Mul']:
            # For regular BN
            # mul_1 <-- mul <-- gamma_read <-- gamma_tensor
            assert len(bn_op.inputs) >= 2, _BN_STRUCTURE_ERROR_MSG
            mul = bn_op.inputs[1].op
            assert len(mul.inputs) >= 2, _BN_STRUCTURE_ERROR_MSG
            gamma_read = mul.inputs[1].op
        elif bn_op.type in ['FusedBatchNormV3']:
            assert len(bn_op.inputs) == 5
            gamma_read = bn_op.inputs[constants.BN_OP_PARAM_INDICES['gamma']].op
            if gamma_read.type == 'Switch':      # tf slim bn using training tensor form
                gamma_read = gamma_read.inputs[0].op
                assert 'read' in gamma_read.name or gamma_read.type == 'Const'
        else:
            logger.error("Error, unknown BN op")
            assert False
        assert gamma_read.type in ['ReadVariableOp', 'Identity', 'Const']    # Will be identity for tf slim BNs
        return gamma_read

    @staticmethod
    def get_gamma_read_var_op_tensor(bn_op: tf.Operation)->tf.Tensor:
        """

        :param bn_op:
        :return:
        """
        assert bn_op.type in ['FusedBatchNormV3', 'Mul']
        gamma_read_tensor = BNUtils.get_gamma_as_read_op(bn_op).outputs[0]
        assert gamma_read_tensor is not None

        if gamma_read_tensor.op.inputs and gamma_read_tensor.op.inputs[0].op.type == 'Switch':
            logger.debug('Fetching params from trainable BN op type')
            gamma_read_tensor = BNUtils._get_tensor_read_var_op_trainable_bn_op(gamma_read_tensor)

        return gamma_read_tensor

    @staticmethod
    def get_gamma_as_numpy_data(sess: tf.Session, bn_op: tf.Operation):
        """
        get gamma param from BN op specified
        :param sess: tensorflow session
        :param bn_op: bn_op obtained from connected graph using get_modules
        (is mul_1 op inside BN scope)
        :return: gamma as numpy data
        """
        try:
            # try name based tensor look up for Keras layers
            gamma_tensor = BNUtils._get_bn_param_tensor_using_name(sess, bn_op,
                                                                   constants.BNOpParamType.gamma)
        except KeyError:
            # if we can't find the tensor name, use structure match
            # to figure out the read tensor for param
            gamma_tensor = BNUtils.get_gamma_read_var_op_tensor(bn_op)

        with sess.graph.as_default():
            numpy_data = sess.run(gamma_tensor)

        return numpy_data

    @staticmethod
    def _bn_op_var_struct_1(bn_op: tf.Operation):
        """
        Return moving_variance op corresponding to batchnorm with training tensor
        :param bn_op: bn_op obtained from connected graph using get_modules
        a mul_1 op inside BN scope.
        :return: Read operation for moving_variance
        """
        try:
            mul_op = bn_op.inputs[1].op
            assert mul_op.type == 'Mul'
            rsqrt_op = mul_op.inputs[0].op
            assert rsqrt_op.type == 'Rsqrt'
            add_op = rsqrt_op.inputs[0].op
            assert add_op.type == 'AddV2'
            merge_op = add_op.inputs[0].op
            assert merge_op.type == 'Merge'
            read_op = merge_op.inputs[0].op
            assert read_op.type in ['ReadVariableOp']
            return read_op
        except:     # pylint: disable=bare-except
            return None

    @staticmethod
    def _bn_op_var_struct_2(bn_op: tf.Operation):
        """
        Return moving_variance op corresponding to batchnorm with training=True
        :param bn_op: bn_op obtained from connected graph using get_modules
        a mul_1 op inside BN scope.
        :return: Read operation for moving_variance
        """
        try:
            mul_op = bn_op.inputs[1].op
            assert mul_op.type == 'Mul'
            rsqrt_op = mul_op.inputs[0].op
            assert rsqrt_op.type == 'Rsqrt'
            add_op = rsqrt_op.inputs[0].op
            assert add_op.type == 'AddV2'
            squeeze_1_op = add_op.inputs[0].op
            assert squeeze_1_op.type == 'Squeeze'
            sub_op = squeeze_1_op.outputs[0].consumers()[0]
            assert sub_op.type == 'Sub'
            read_op = sub_op.inputs[0].op
            assert read_op.type in ['ReadVariableOp']
            return read_op
        except:     # pylint: disable=bare-except
            return None

    @staticmethod
    def _bn_op_var_struct_3(bn_op: tf.Operation):
        """
        Return moving_variance op corresponding to batchnorm with training=False
        :param bn_op: bn_op obtained from connected graph using get_modules
        a mul_1 op inside BN scope.
        :return: Read operation for moving_variance
        """
        try:
            mul_op = bn_op.inputs[1].op
            assert mul_op.type == 'Mul'
            rsqrt_op = mul_op.inputs[0].op
            assert rsqrt_op.type == 'Rsqrt'
            add_op = rsqrt_op.inputs[0].op
            assert add_op.type == 'AddV2'
            read_op = add_op.inputs[0].op
            assert read_op.type in ['ReadVariableOp']
            return read_op
        except:     # pylint: disable=bare-except
            return None

    @staticmethod
    def get_moving_variance_as_read_op(bn_op: tf.Operation):
        """
        get moving variance read op from BN op specified
        :param bn_op: bn_op obtained from connected graph using get_modules
        a mul_1 op inside BN scope.
        :return: moving variance as read op
        """
        # register handlers for different structures
        bn_op_struct_for_variance_handlers = [BNUtils._bn_op_var_struct_1,
                                              BNUtils._bn_op_var_struct_2,
                                              BNUtils._bn_op_var_struct_3]

        if bn_op.type in ['FusedBatchNormV3']:
            assert len(bn_op.inputs) == 5
            moving_var_read = bn_op.inputs[constants.BN_OP_PARAM_INDICES['movingvariance']].op
            if moving_var_read.type == 'Switch':      # tf slim bn using training tensor form
                moving_var_read = moving_var_read.inputs[0].op
                assert 'read' in moving_var_read.name
        elif bn_op.type in ['Mul']:
            # For regular BN
            moving_var_read = None
            # try all handlers available
            for handler in bn_op_struct_for_variance_handlers:
                if moving_var_read is None:
                    moving_var_read = handler(bn_op)
                else:
                    break
            assert moving_var_read is not None, _BN_STRUCTURE_ERROR_MSG
        else:
            logger.error("Error, unknown BN op")
            assert False

        if moving_var_read.type == 'Identity':
            assert len(moving_var_read.inputs) == 1, _BN_STRUCTURE_ERROR_MSG

        assert moving_var_read.type in ['ReadVariableOp', 'Const', 'Identity']
        return moving_var_read

    @staticmethod
    def get_moving_variance_read_var_op_tensor(bn_op: tf.Operation)->tf.Tensor:
        """
        get moving variance readVariableOp tensor from BN op specified
        :param bn_op: FusedBatchNorm as tf.Operation
        :return: tensor associated with bn op moving variance readVariableOp type, as tf.Tensor
        """
        # only support fused BN
        assert bn_op.type in ['FusedBatchNormV3', 'Mul']
        moving_var_read_tensor = BNUtils.get_moving_variance_as_read_op(bn_op).outputs[0]
        assert moving_var_read_tensor is not None

        if moving_var_read_tensor.op.type == 'Const':
            logger.debug("BN op has const type op for moving variance")

            # get the sub_1 op associated with moving variance read op
            assert len(bn_op.outputs) >= 2
            moving_avg_1_sub_1 = bn_op.outputs[2].consumers()[0]
            all_inputs = moving_avg_1_sub_1.inputs

            # among inputs figure out the read var op type that can be "evaluated"
            for input_t in all_inputs:
                if input_t.op.type == 'ReadVariableOp':
                    moving_var_read_tensor = input_t
                elif input_t.op.type == 'Identity' and 'read:0' in input_t.name:      # tf slim form
                    moving_var_read_tensor = input_t

        elif moving_var_read_tensor.op.inputs[0].op.type == 'Switch':
            logger.debug("Fetch moving var from a trainable BN op structure")
            moving_var_read_tensor = BNUtils._get_tensor_read_var_op_trainable_bn_op(moving_var_read_tensor)

        return moving_var_read_tensor

    @staticmethod
    def get_moving_variance_as_numpy_data(sess: tf.Session, bn_op: tf.Operation):
        """
        get moving variance param from BN op specified
        :param sess: tensorflow session
        :param bn_op: bn_op obtained from connected graph using get_modules
        a mul_1 op inside BN scope.
        :return: moving variance as numpy data
        """

        try:
            # try name based tensor look up for Keras layers
            moving_var_tensor = BNUtils._get_bn_param_tensor_using_name(sess, bn_op,
                                                                        constants.BNOpParamType.moving_variance)
        except KeyError:
            # if we can't find the tensor name, use structure match
            # to figure out the read tensor for param
            moving_var_tensor = BNUtils.get_moving_variance_read_var_op_tensor(bn_op)

        with sess.graph.as_default():
            numpy_data = sess.run(moving_var_tensor)
        return numpy_data

    @staticmethod
    def _bn_op_mean_struct_1(bn_op: tf.Operation):
        """
        Return moving_mean op corresponding to batchnorm with training tensor
        :param bn_op: bn_op obtained from connected graph using get_modules
        a mul_1 op inside BN scope.
        :return: Read operation for moving_mean
        """
        try:
            mul_op = bn_op.inputs[1].op
            assert mul_op.type == 'Mul'
            mul_2_op = mul_op.outputs[0].consumers()[1]
            assert mul_2_op.type == 'Mul'
            merge_op = mul_2_op.inputs[0].op
            assert merge_op.type == 'Merge'
            read_op = merge_op.inputs[0].op
            assert read_op.type in ['ReadVariableOp']
            return read_op
        except:     # pylint: disable=bare-except
            return None

    @staticmethod
    def _bn_op_mean_struct_2(bn_op: tf.Operation):
        """
        Return moving_mean op corresponding to batchnorm with training=True
        :param bn_op: bn_op obtained from connected graph using get_modules
        a mul_1 op inside BN scope.
        :return: Read operation for moving_mean
        """
        try:
            mul_op = bn_op.inputs[1].op
            assert mul_op.type == 'Mul'
            mul_2_op = mul_op.outputs[0].consumers()[1]
            assert mul_2_op.type == 'Mul'
            squeeze_op = mul_2_op.inputs[0].op
            assert squeeze_op.type == 'Squeeze'
            sub_op = squeeze_op.outputs[0].consumers()[0]
            assert sub_op.type == 'Sub'
            read_op = sub_op.inputs[0].op
            assert read_op.type in ['ReadVariableOp']
            return read_op
        except:     # pylint: disable=bare-except
            return None

    @staticmethod
    def _bn_op_mean_struct_3(bn_op: tf.Operation):
        """
        Return moving_mean op corresponding to batchnorm with training=False
        :param bn_op: bn_op obtained from connected graph using get_modules
        a mul_1 op inside BN scope.
        :return: Read operation for moving_mean
        """
        try:
            mul_op = bn_op.inputs[1].op
            assert mul_op.type == 'Mul'
            mul_2_op = mul_op.outputs[0].consumers()[1]
            assert mul_2_op.type == 'Mul'
            read_op = mul_2_op.inputs[0].op
            assert read_op.type in ['ReadVariableOp']
            return read_op
        except:     # pylint: disable=bare-except
            return None

    @staticmethod
    def get_moving_mean_as_read_op(bn_op: tf.Operation):
        """
        get moving mean read op from BN op specified
        :param bn_op: bn_op obtained from connected graph using get_modules
        a mul_1 op inside BN scope.
        :return: moving mean read op
        """
        if bn_op.type in ['FusedBatchNormV3']:
            assert len(bn_op.inputs) == 5
            moving_mean_read = bn_op.inputs[constants.BN_OP_PARAM_INDICES['movingmean']].op
            if moving_mean_read.type == 'Switch':      # tf slim bn using training tensor form
                moving_mean_read = moving_mean_read.inputs[0].op
                assert 'read' in moving_mean_read.name
        elif bn_op.type in ['Mul']:
            # For regular BN
            # mul_1 << - mul --> mul_2 <-- cond/merge <-- switch2 <-- moving mean read < moving mean tensor
            # inputs[1] is mul .op.inputs[1] is gamma:read op whose input is gamma tensor as variable v2

            # register handlers for different structures
            bn_op_struct_for_mean_handlers = [BNUtils._bn_op_mean_struct_1,
                                              BNUtils._bn_op_mean_struct_2,
                                              BNUtils._bn_op_mean_struct_3]

            moving_mean_read = None
            # try all handlers available
            for handler in bn_op_struct_for_mean_handlers:
                if moving_mean_read is None:
                    moving_mean_read = handler(bn_op)
                else:
                    break
            assert moving_mean_read is not None, _BN_STRUCTURE_ERROR_MSG
        else:
            logger.error("Error, unknown BN op")
            assert False

        if moving_mean_read.type == 'Identity':
            assert len(moving_mean_read.inputs) == 1, _BN_STRUCTURE_ERROR_MSG

        assert moving_mean_read.type in ['ReadVariableOp', 'Const', 'Identity']
        return moving_mean_read

    @staticmethod
    def get_moving_mean_read_var_op_tensor(bn_op: tf.Operation)->tf.Tensor:
        """
        get moving mean readVariableOp tensor from BN op specified
        :param bn_op: FusedBatchNorm as tf.Operation
        :return: tensor associated with bn op moving mean readVariableOp type, as tf.Tensor
        """
        # only support fused BN
        assert bn_op.type in ['FusedBatchNormV3', 'Mul']
        moving_mean_read_tensor = BNUtils.get_moving_mean_as_read_op(bn_op).outputs[0]
        assert moving_mean_read_tensor is not None

        if moving_mean_read_tensor.op.type == 'Const':
            logger.debug("BN op has const type op for moving variance")
            # get the read var type from bn op
            # get the sub_1 op associated with moving mean read op
            assert len(bn_op.outputs) > 1
            moving_avg_sub_1 = bn_op.outputs[1].consumers()[0]
            all_inputs = moving_avg_sub_1.inputs

            # among inputs figure out the read var op type that can be "evaluated"
            for input_t in all_inputs:
                if input_t.op.type == 'ReadVariableOp':
                    moving_mean_read_tensor = input_t
                elif input_t.op.type == 'Identity' and 'read:0' in input_t.name:      # tf slim form
                    moving_mean_read_tensor = input_t

        elif moving_mean_read_tensor.op.inputs[0].op.type == 'Switch':
            logger.debug("Fetch moving var from a trainable BN op structure")
            moving_mean_read_tensor = BNUtils._get_tensor_read_var_op_trainable_bn_op(moving_mean_read_tensor)

        return moving_mean_read_tensor

    @staticmethod
    def get_moving_mean_as_numpy_data(sess: tf.Session, bn_op: tf.Operation):
        """
        get moving mean param from BN op specified
        :param sess: tensorflow session
        :param bn_op: bn_op obtained from connected graph using get_modules
        a mul_1 op inside BN scope.
        :return: moving mean as numpy data
        """

        try:
            # try name based tensor look up for Keras layers
            moving_mean_tensor = BNUtils._get_bn_param_tensor_using_name(sess, bn_op,
                                                                         constants.BNOpParamType.moving_mean)
        except KeyError:
            # if we can't find the tensor name, use structure match
            # to figure out the read tensor for param
            moving_mean_tensor = BNUtils.get_moving_mean_read_var_op_tensor(bn_op)

        with sess.graph.as_default():
            numpy_data = sess.run(moving_mean_tensor)
        return numpy_data

    @staticmethod
    def get_epsilon(bn_op: tf.Operation):
        """
        Returns epsilon extracted from given bn ip
        :param bn_op: bn_op obtained from connected graph using get_modules
        a mul_1 op inside BN scope.
        :return: epsilon value
        """

        # only support fused BNs
        assert bn_op.type == 'FusedBatchNormV3'
        # epsilon can be derived as attribute value
        numpy_epsilon = bn_op.get_attr("epsilon")

        return numpy_epsilon

    @staticmethod
    def get_assign_moving_avg_op(bn_op: tf.Operation) -> Union[tf.Operation, None]:
        """
        Get assign_moving_avg op corresponding with the bn_op, if it exists.
        :param bn_op: Batchnorm op to search for corresponding assign_moving_avg op
        :return: assign_moving_op corresponding with the bn op, or None if it does not exist.
        """
        assert bn_op.type == 'FusedBatchNormV3'
        assert len(bn_op.outputs) == 6
        if bn_op.outputs[1].consumers():
            child_op = bn_op.outputs[1].consumers()[0]
            if child_op.type == 'Merge':
                sub_op = child_op.outputs[0].consumers()[0]
            else:
                sub_op = child_op
            assert sub_op.type == 'Sub'
            mul_op = sub_op.outputs[0].consumers()[0]
            assert mul_op.type == 'Mul'
            assign_moving_avg_op = mul_op.outputs[0].consumers()[0]
            assert assign_moving_avg_op.type in ['AssignSub', 'AssignSubVariableOp']
            return assign_moving_avg_op
        return None

    @staticmethod
    def get_assign_moving_avg_1_op(bn_op: tf.Operation) -> Union[tf.Operation, None]:
        """
        Get assign_moving_avg_1 op corresponding with the bn_op, if it exists.
        :param bn_op: Batchnorm op to search for corresponding assign_moving_avg_1 op
        :return: assign_moving_avg_1 corresponding with the bn op, or None if it does not exist.
        """
        assert bn_op.type == 'FusedBatchNormV3'
        assert len(bn_op.outputs) == 6
        if bn_op.outputs[2].consumers():
            child_op = bn_op.outputs[2].consumers()[0]
            if child_op.type == 'Merge':
                sub_op = child_op.outputs[0].consumers()[0]
            else:
                sub_op = child_op
            assert sub_op.type == 'Sub'
            mul_op = sub_op.outputs[0].consumers()[0]
            assert mul_op.type == 'Mul'
            assign_moving_avg_op = mul_op.outputs[0].consumers()[0]
            assert assign_moving_avg_op.type in ['AssignSub', 'AssignSubVariableOp']
            return assign_moving_avg_op
        return None

    @staticmethod
    def remove_bn_op_from_update_ops(sess: tf.Session, bn_op: tf.Operation):
        """
        Remove batchnorm assign_moving_avg and assign_moving_avg_1 ops from update ops.
        :param sess: tf Session
        :param bn_op: BatchNorm operation whose assign_moving_avg and assign_moving_avg_1 ops should be removed.
        """
        with sess.graph.as_default():
            update_ops = tf.get_collection_ref(tf.GraphKeys.UPDATE_OPS)
            assign_moving_avg_op = BNUtils.get_assign_moving_avg_op(bn_op)
            assign_moving_avg_op_1 = BNUtils.get_assign_moving_avg_1_op(bn_op)
            if assign_moving_avg_op and assign_moving_avg_op in update_ops:
                update_ops.remove(assign_moving_avg_op)
                logger.debug('Removed %s from update ops', assign_moving_avg_op.name)
            if assign_moving_avg_op_1 and assign_moving_avg_op_1 in update_ops:
                update_ops.remove(assign_moving_avg_op_1)
                logger.debug('Removed %s from update ops', assign_moving_avg_op_1.name)

    @staticmethod
    def _get_bn_param_tensor_using_name(sess: tf.Session, bn_op: tf.Operation, param_type: constants.BNOpParamType):
        """
        Helper to get BN op param read tensor
        :param sess: TensorFlow session tf.Session
        :param bn_op: BN op from which param read tensor is to be extracted
        :param param_type: param type for which param tensor is to be
        extracted, as constants.BNOpParamType (supported types are beta,
        gamma, moving_mean or moving_variance)
        :return: param read tensor
        """

        if param_type not in vars(constants.BNOpParamType).values():
            assert 0, 'Error, get_bn_param_using_name() invalid param type requested'

        # name of the fused bn contains bn_name/FusedBatchNormV3 or
        # bn_name/cond/FusedBatchNormV3_1
        # we need only the bn_name to make param tensor names
        op_name = bn_op.name.split('/')[0]
        param_tensor_name = op_name + constants.BN_OP_PARAM_NAME_SUFFIX[param_type]
        param_tensor = sess.graph.get_tensor_by_name(param_tensor_name)

        return param_tensor
