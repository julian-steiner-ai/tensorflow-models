# Copyright 2022 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Contains definitions of dense prediction heads."""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Mapping, Optional, Union, Type

# Import libraries

import numpy as np
import tensorflow as tf

import tensorflow_model_optimization as tfmot
from official.modeling import tf_utils
from official.projects.qat.vision.quantization import configs
from official.projects.qat.vision.quantization import helper


class SeparableConv2DQuantized(tf.keras.layers.Layer):
  """Quantized SeperableConv2D."""

  def __init__(self,
               name: Optional[str] = None,
               last_quantize: bool = False,
               **conv_kwargs):
    """Initializes a SeparableConv2DQuantized.

    Args:
      name: The name of the layer.
      last_quantize: A `bool` indicates whether add quantization for the output.
      **conv_kwargs: A keyword arguments to be used for conv and dwconv.
    """

    super().__init__(name=name)
    self._conv_kwargs = copy.deepcopy(conv_kwargs)
    self._name = name
    self._last_quantize = last_quantize

  def build(self, input_shape: Union[tf.TensorShape, List[tf.TensorShape]]):
    """Creates the child layers of the layer."""
    depthwise_conv2d_quantized = helper.quantize_wrapped_layer(
        tf.keras.layers.DepthwiseConv2D,
        configs.Default8BitConvQuantizeConfig(
            ['depthwise_kernel'], [], True))
    conv2d_quantized = helper.quantize_wrapped_layer(
        tf.keras.layers.Conv2D,
        configs.Default8BitConvQuantizeConfig(
            ['kernel'], [], self._last_quantize))

    dwconv_kwargs = self._conv_kwargs.copy()
    # Depthwise conv input filters is always equal to output filters.
    # This filters argument only needed for the point-wise conv2d op.
    del dwconv_kwargs['filters']
    dwconv_kwargs.update({
        'activation': None,
        'use_bias': False,
    })
    self.dw_conv = depthwise_conv2d_quantized(name='dw', **dwconv_kwargs)

    conv_kwargs = self._conv_kwargs.copy()
    conv_kwargs.update({
        'kernel_size': (1, 1),
        'strides': (1, 1),
        'padding': 'valid',
        'groups': 1,
    })

    self.conv = conv2d_quantized(name='pw', **conv_kwargs)

  def call(self, inputs: tf.Tensor) -> tf.Tensor:
    """Call the separable conv layer."""
    x = self.dw_conv(inputs)
    outputs = self.conv(x)
    return outputs

  def get_config(self) -> Dict[str, Any]:
    """Returns the config of the layer."""
    config = self._conv_kwargs.copy()
    config.update({
        'name': self._name,
        'last_quantize': self._last_quantize,
    })
    return config

  @classmethod
  def from_config(
      cls: Type[SeparableConv2DQuantized],
      config: Dict[str, Any]) -> SeparableConv2DQuantized:
    """Creates a layer from its config."""
    return cls(**config)


@tf.keras.utils.register_keras_serializable(package='Vision')
class RetinaNetHeadQuantized(tf.keras.layers.Layer):
  """Creates a RetinaNet quantized head."""

  def __init__(
      self,
      min_level: int,
      max_level: int,
      num_classes: int,
      num_anchors_per_location: int,
      num_convs: int = 4,
      num_filters: int = 256,
      attribute_heads: Optional[List[Dict[str, Any]]] = None,
      use_separable_conv: bool = False,
      activation: str = 'relu',
      use_sync_bn: bool = False,
      norm_momentum: float = 0.99,
      norm_epsilon: float = 0.001,
      kernel_regularizer: Optional[tf.keras.regularizers.Regularizer] = None,
      bias_regularizer: Optional[tf.keras.regularizers.Regularizer] = None,
      num_params_per_anchor: int = 4,
      **kwargs):
    """Initializes a RetinaNet quantized head.

    Args:
      min_level: An `int` number of minimum feature level.
      max_level: An `int` number of maximum feature level.
      num_classes: An `int` number of classes to predict.
      num_anchors_per_location: An `int` number of number of anchors per pixel
        location.
      num_convs: An `int` number that represents the number of the intermediate
        conv layers before the prediction.
      num_filters: An `int` number that represents the number of filters of the
        intermediate conv layers.
      attribute_heads: If not None, a list that contains a dict for each
        additional attribute head. Each dict consists of 3 key-value pairs:
        `name`, `type` ('regression' or 'classification'), and `size` (number
        of predicted values for each instance).
      use_separable_conv: A `bool` that indicates whether the separable
        convolution layers is used.
      activation: A `str` that indicates which activation is used, e.g. 'relu',
        'swish', etc.
      use_sync_bn: A `bool` that indicates whether to use synchronized batch
        normalization across different replicas.
      norm_momentum: A `float` of normalization momentum for the moving average.
      norm_epsilon: A `float` added to variance to avoid dividing by zero.
      kernel_regularizer: A `tf.keras.regularizers.Regularizer` object for
        Conv2D. Default is None.
      bias_regularizer: A `tf.keras.regularizers.Regularizer` object for Conv2D.
      num_params_per_anchor: Number of parameters required to specify an anchor
        box. For example, `num_params_per_anchor` would be 4 for axis-aligned
        anchor boxes specified by their y-centers, x-centers, heights, and
        widths.
      **kwargs: Additional keyword arguments to be passed.
    """
    super().__init__(**kwargs)
    self._config_dict = {
        'min_level': min_level,
        'max_level': max_level,
        'num_classes': num_classes,
        'num_anchors_per_location': num_anchors_per_location,
        'num_convs': num_convs,
        'num_filters': num_filters,
        'attribute_heads': attribute_heads,
        'use_separable_conv': use_separable_conv,
        'activation': activation,
        'use_sync_bn': use_sync_bn,
        'norm_momentum': norm_momentum,
        'norm_epsilon': norm_epsilon,
        'kernel_regularizer': kernel_regularizer,
        'bias_regularizer': bias_regularizer,
        'num_params_per_anchor': num_params_per_anchor,
    }

    if tf.keras.backend.image_data_format() == 'channels_last':
      self._bn_axis = -1
    else:
      self._bn_axis = 1
    self._activation = tfmot.quantization.keras.QuantizeWrapperV2(
        tf_utils.get_activation(activation, use_keras_layer=True),
        configs.Default8BitActivationQuantizeConfig())

  def build(self, input_shape: Union[tf.TensorShape, List[tf.TensorShape]]):
    """Creates the variables of the head."""
    if self._config_dict['use_separable_conv']:
      conv_op = SeparableConv2DQuantized
    else:
      conv_op = helper.quantize_wrapped_layer(
          tf.keras.layers.Conv2D,
          configs.Default8BitConvQuantizeConfig(
              ['kernel'], ['activation'], False))
    conv_kwargs = {
        'filters': self._config_dict['num_filters'],
        'kernel_size': 3,
        'padding': 'same',
        'bias_initializer': tf.zeros_initializer(),
        'bias_regularizer': self._config_dict['bias_regularizer'],
    }
    if not self._config_dict['use_separable_conv']:
      conv_kwargs.update({
          'kernel_initializer': tf.keras.initializers.RandomNormal(
              stddev=0.01),
          'kernel_regularizer': self._config_dict['kernel_regularizer'],
      })

    base_bn_op = (tf.keras.layers.experimental.SyncBatchNormalization
                  if self._config_dict['use_sync_bn']
                  else tf.keras.layers.BatchNormalization)
    bn_op = helper.norm_by_activation(
        self._config_dict['activation'],
        helper.quantize_wrapped_layer(
            base_bn_op, configs.Default8BitOutputQuantizeConfig()),
        helper.quantize_wrapped_layer(
            base_bn_op, configs.NoOpQuantizeConfig()))

    bn_kwargs = {
        'axis': self._bn_axis,
        'momentum': self._config_dict['norm_momentum'],
        'epsilon': self._config_dict['norm_epsilon'],
    }

    # Class net.
    self._cls_convs = []
    self._cls_norms = []
    for level in range(
        self._config_dict['min_level'], self._config_dict['max_level'] + 1):
      this_level_cls_norms = []
      for i in range(self._config_dict['num_convs']):
        if level == self._config_dict['min_level']:
          cls_conv_name = 'classnet-conv_{}'.format(i)
          self._cls_convs.append(conv_op(name=cls_conv_name, **conv_kwargs))
        cls_norm_name = 'classnet-conv-norm_{}_{}'.format(level, i)
        this_level_cls_norms.append(bn_op(name=cls_norm_name, **bn_kwargs))
      self._cls_norms.append(this_level_cls_norms)

    classifier_kwargs = {
        'filters': (
            self._config_dict['num_classes'] *
            self._config_dict['num_anchors_per_location']),
        'kernel_size': 3,
        'padding': 'same',
        'bias_initializer': tf.constant_initializer(-np.log((1 - 0.01) / 0.01)),
        'bias_regularizer': self._config_dict['bias_regularizer'],
    }
    if not self._config_dict['use_separable_conv']:
      classifier_kwargs.update({
          'kernel_initializer': tf.keras.initializers.RandomNormal(stddev=1e-5),
          'kernel_regularizer': self._config_dict['kernel_regularizer'],
      })
    self._classifier = conv_op(
        name='scores', last_quantize=True, **classifier_kwargs)

    # Box net.
    self._box_convs = []
    self._box_norms = []
    for level in range(
        self._config_dict['min_level'], self._config_dict['max_level'] + 1):
      this_level_box_norms = []
      for i in range(self._config_dict['num_convs']):
        if level == self._config_dict['min_level']:
          box_conv_name = 'boxnet-conv_{}'.format(i)
          self._box_convs.append(conv_op(name=box_conv_name, **conv_kwargs))
        box_norm_name = 'boxnet-conv-norm_{}_{}'.format(level, i)
        this_level_box_norms.append(bn_op(name=box_norm_name, **bn_kwargs))
      self._box_norms.append(this_level_box_norms)

    box_regressor_kwargs = {
        'filters': (self._config_dict['num_params_per_anchor'] *
                    self._config_dict['num_anchors_per_location']),
        'kernel_size': 3,
        'padding': 'same',
        'bias_initializer': tf.zeros_initializer(),
        'bias_regularizer': self._config_dict['bias_regularizer'],
    }
    if not self._config_dict['use_separable_conv']:
      box_regressor_kwargs.update({
          'kernel_initializer': tf.keras.initializers.RandomNormal(
              stddev=1e-5),
          'kernel_regularizer': self._config_dict['kernel_regularizer'],
      })
    self._box_regressor = conv_op(
        name='boxes', last_quantize=True, **box_regressor_kwargs)

    # Attribute learning nets.
    if self._config_dict['attribute_heads']:
      self._att_predictors = {}
      self._att_convs = {}
      self._att_norms = {}

      for att_config in self._config_dict['attribute_heads']:
        att_name = att_config['name']
        att_type = att_config['type']
        att_size = att_config['size']
        att_convs_i = []
        att_norms_i = []

        # Build conv and norm layers.
        for level in range(self._config_dict['min_level'],
                           self._config_dict['max_level'] + 1):
          this_level_att_norms = []
          for i in range(self._config_dict['num_convs']):
            if level == self._config_dict['min_level']:
              att_conv_name = '{}-conv_{}'.format(att_name, i)
              att_convs_i.append(conv_op(name=att_conv_name, **conv_kwargs))
            att_norm_name = '{}-conv-norm_{}_{}'.format(att_name, level, i)
            this_level_att_norms.append(bn_op(name=att_norm_name, **bn_kwargs))
          att_norms_i.append(this_level_att_norms)
        self._att_convs[att_name] = att_convs_i
        self._att_norms[att_name] = att_norms_i

        # Build the final prediction layer.
        att_predictor_kwargs = {
            'filters':
                (att_size * self._config_dict['num_anchors_per_location']),
            'kernel_size': 3,
            'padding': 'same',
            'bias_initializer': tf.zeros_initializer(),
            'bias_regularizer': self._config_dict['bias_regularizer'],
        }
        if att_type == 'regression':
          att_predictor_kwargs.update(
              {'bias_initializer': tf.zeros_initializer()})
        elif att_type == 'classification':
          att_predictor_kwargs.update({
              'bias_initializer':
                  tf.constant_initializer(-np.log((1 - 0.01) / 0.01))
          })
        else:
          raise ValueError(
              'Attribute head type {} not supported.'.format(att_type))

        if not self._config_dict['use_separable_conv']:
          att_predictor_kwargs.update({
              'kernel_initializer':
                  tf.keras.initializers.RandomNormal(stddev=1e-5),
              'kernel_regularizer':
                  self._config_dict['kernel_regularizer'],
          })

        self._att_predictors[att_name] = conv_op(
            name='{}_attributes'.format(att_name), **att_predictor_kwargs)

    super().build(input_shape)

  def call(self, features: Mapping[str, tf.Tensor]):
    """Forward pass of the RetinaNet quantized head.

    Args:
      features: A `dict` of `tf.Tensor` where
        - key: A `str` of the level of the multilevel features.
        - values: A `tf.Tensor`, the feature map tensors, whose shape is
            [batch, height_l, width_l, channels].

    Returns:
      scores: A `dict` of `tf.Tensor` which includes scores of the predictions.
        - key: A `str` of the level of the multilevel predictions.
        - values: A `tf.Tensor` of the box scores predicted from a particular
            feature level, whose shape is
            [batch, height_l, width_l, num_classes * num_anchors_per_location].
      boxes: A `dict` of `tf.Tensor` which includes coordinates of the
        predictions.
        - key: A `str` of the level of the multilevel predictions.
        - values: A `tf.Tensor` of the box scores predicted from a particular
            feature level, whose shape is
            [batch, height_l, width_l,
             num_params_per_anchor * num_anchors_per_location].
      attributes: a dict of (attribute_name, attribute_prediction). Each
        `attribute_prediction` is a dict of:
        - key: `str`, the level of the multilevel predictions.
        - values: `Tensor`, the box scores predicted from a particular feature
            level, whose shape is
            [batch, height_l, width_l,
            attribute_size * num_anchors_per_location].
        Can be an empty dictionary if no attribute learning is required.
    """
    scores = {}
    boxes = {}
    if self._config_dict['attribute_heads']:
      attributes = {
          att_config['name']: {}
          for att_config in self._config_dict['attribute_heads']
      }
    else:
      attributes = {}

    for i, level in enumerate(
        range(self._config_dict['min_level'],
              self._config_dict['max_level'] + 1)):
      this_level_features = features[str(level)]

      # class net.
      x = this_level_features
      for conv, norm in zip(self._cls_convs, self._cls_norms[i]):
        x = conv(x)
        x = norm(x)
        x = self._activation(x)
      scores[str(level)] = self._classifier(x)

      # box net.
      x = this_level_features
      for conv, norm in zip(self._box_convs, self._box_norms[i]):
        x = conv(x)
        x = norm(x)
        x = self._activation(x)
      boxes[str(level)] = self._box_regressor(x)

      # attribute nets.
      if self._config_dict['attribute_heads']:
        for att_config in self._config_dict['attribute_heads']:
          att_name = att_config['name']
          x = this_level_features
          for conv, norm in zip(self._att_convs[att_name],
                                self._att_norms[att_name][i]):
            x = conv(x)
            x = norm(x)
            x = self._activation(x)
          attributes[att_name][str(level)] = self._att_predictors[att_name](x)

    return scores, boxes, attributes

  def get_config(self):
    return self._config_dict

  @classmethod
  def from_config(cls, config):
    return cls(**config)

