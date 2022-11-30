# /usr/bin/env python3.5
# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2021-2022, Qualcomm Innovation Center, Inc. All rights reserved.
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

""" Unit tests for keras model preparer """
import os

import numpy as np
import tensorflow as tf
from aimet_tensorflow.keras.model_preparer import prepare_model

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'


def conv_functional():
    input_shape = (128, 28, 28, 1)
    inp = tf.keras.Input(shape=input_shape[1:])
    x = tf.keras.layers.Conv2D(32, kernel_size=(3, 3), activation="relu")(inp)
    x = tf.keras.layers.Conv2DTranspose(
        32, kernel_size=(3, 3), activation="relu")(x)
    x = tf.keras.layers.DepthwiseConv2D(
        depth_multiplier=1, kernel_size=(3, 3), activation='relu')(x)
    x = tf.keras.layers.Flatten()(x)
    x = tf.keras.layers.Dropout(0.5, trainable=False)(x)
    x = tf.keras.layers.Dense(10, activation="softmax")(x)

    model = tf.keras.Model(inputs=inp, outputs=x, name='conv_functional')
    return model


# Not used for testing at the moment. This is placed here for future testing.
class ConvTimesThree(tf.keras.layers.Layer):
    def __init__(self, use_nested_calls=False, **kwargs):
        super(ConvTimesThree, self).__init__(**kwargs)
        self.conv = tf.keras.layers.Conv2D(32,
                                           kernel_size=(3, 3),
                                           activation='relu',
                                           name='class_conv')

        self.conv_transpose = tf.keras.layers.Conv2DTranspose(64,
                                                              kernel_size=(3, 3),
                                                              activation='relu',
                                                              name='class_conv_transpose')

        self.depth_conv = tf.keras.layers.DepthwiseConv2D(depth_multiplier=1,
                                                          kernel_size=(3, 3),
                                                          activation='relu',
                                                          name='class_conv_depth')

        self.use_nested_calls = use_nested_calls

    def call(self, x):
        if self.use_nested_calls and (2 + 2 == 4):
            x = self.conv_transpose(self.conv(x))
        else:
            x = self.conv(x)
            x = self.conv_transpose(x)
        return self.depth_conv(x)

# See comment above ConvTimesThree Class


def conv_sub_class():
    input_shape = (128, 28, 28, 1)
    inp = tf.keras.Input(batch_shape=input_shape)
    x = ConvTimesThree()(inp)
    x = tf.keras.layers.Flatten()(x)
    x = tf.keras.layers.Dropout(0.5)(x, training=False)
    x = tf.keras.layers.Dense(10, activation="softmax")(x)

    model = tf.keras.Model(inputs=inp, outputs=x, name='conv_classes')
    return model

# Below models are based on Deep Learning with Python by Francois Chollet Second Edition (page 182 - 185)
# Only Subclassing


class CustomerTicketModel(tf.keras.Model):

    def __init__(self, num_departments):
        super().__init__()
        self.concat_layer = tf.keras.layers.Concatenate()
        self.mixing_layer = tf.keras.layers.Dense(64, activation="relu")
        self.priority_scorer = tf.keras.layers.Dense(1, activation="sigmoid")
        self.department_classifier = tf.keras.layers.Dense(num_departments, activation="softmax")

    def call(self, inputs):
        title = inputs["title"]
        text_body = inputs["text_body"]
        tags = inputs["tags"]

        features = self.concat_layer([title, text_body, tags])
        features = self.mixing_layer(features)
        priority = self.priority_scorer(features)
        department = self.department_classifier(features)
        return priority, department

# Functional model that includes subclassed layers


class Classifier(tf.keras.Model):

    def __init__(self, num_classes=4):
        super().__init__()
        if num_classes == 2:
            num_units = 1
            activation = "sigmoid"
        else:
            num_units = num_classes
            activation = "softmax"
        self.dense = tf.keras.layers.Dense(num_units, activation=activation)

    def call(self, inputs):
        return self.dense(inputs)


def functional_model_with_subclassed_layers():
    inputs = tf.keras.layers.Input(shape=(3,))
    features = tf.keras.layers.Dense(64, activation="relu")(inputs)
    outputs = Classifier(num_classes=10)(features)
    model = tf.keras.Model(inputs=inputs, outputs=outputs)
    return model


# Subclass model that includes functional layers

def subclass_model_with_functional_layers():
    inputs = tf.keras.Input(shape=(64,))
    outputs = tf.keras.layers.Dense(1, activation="sigmoid")(inputs)
    binary_classifier = tf.keras.Model(inputs=inputs, outputs=outputs)

    class MyModel(tf.keras.Model):

        def __init__(self, num_classes=2):
            super().__init__()
            self.dense = tf.keras.layers.Dense(64, activation="relu")
            self.classifier = binary_classifier

        def call(self, inputs):
            features = self.dense(inputs)
            return self.classifier(features)

    model = MyModel()
    return model


def compare_weights(original_model, functional_model):
    """
    Helper function to compare the weights of two models. This function is used to test the conversion script.
    :param original_model: the original model
    :param functional_model: the model that was converted from the original model
    """
    original_weights = original_model.get_weights()
    functional_weights = functional_model.get_weights()
    for i in range(len(original_weights)):
        np.testing.assert_array_equal(original_weights[i], functional_weights[i])


def test_full_subclass_to_functional():
    vocabulary_size = 10000
    num_tags = 100
    num_departments = 4
    num_samples = 1280

    title_data = np.random.randint(0, 2, size=(num_samples, vocabulary_size))
    text_body_data = np.random.randint(0, 2, size=(num_samples, vocabulary_size))
    tags_data = np.random.randint(0, 2, size=(num_samples, num_tags))

    model = CustomerTicketModel(num_departments=num_departments)
    _ = model({"title": title_data,
               "text_body": text_body_data,
               "tags": tags_data})
    # Since this model is fully subclassed, specifically at the beginning, we call prepare model with
    # the inputs to have Keras symoblic tensor fit the rest of the layers correctly.
    functional_model = prepare_model(model,
                                     [tf.keras.Input(shape=(num_samples, vocabulary_size,)),
                                      tf.keras.Input(shape=(num_samples, vocabulary_size,)),
                                      tf.keras.Input(shape=(num_samples, num_tags,))])
    assert functional_model.count_params() == model.count_params()
    compare_weights(model, functional_model)


def test_functional_model_with_subclassed_layers_to_functional():
    model = functional_model_with_subclassed_layers()
    random_input = np.random.rand(32, 3)
    _ = model(random_input)

    functional_model = prepare_model(model)
    assert functional_model.count_params() == model.count_params()
    compare_weights(model, functional_model)


def test_subclass_model_with_subclassed_layers_to_functional():
    model = subclass_model_with_functional_layers()
    input_shape = (32, 64)
    random_input = np.random.rand(*input_shape)
    _ = model(random_input)

    functional_model = prepare_model(model, tf.keras.Input(shape=input_shape[1:]))
    assert functional_model.count_params() == model.count_params()
    compare_weights(model, functional_model)


def test_conv_times_three_subclass_to_functional():
    model = conv_sub_class()
    input_shape = (32, 28, 28, 1)
    random_input = np.random.rand(*input_shape)
    _ = model(random_input)

    functional_model = prepare_model(model)
    assert functional_model.count_params() == model.count_params()
    compare_weights(model, functional_model)


test_conv_times_three_subclass_to_functional()
