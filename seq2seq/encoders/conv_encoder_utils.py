# Copyright 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
An encoder that conv over embeddings, as described in
https://arxiv.org/abs/1705.03122.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

class ConvEncoderUtils():
  
  @staticmethod 
  def parse_list_or_default(params_str, number, default_val, delimitor=','):
    param_list = []
    if params_str == "":
      param_list = [default_val] * number
    else:
      param_list = [int(x) for x in params_str.strip().split(delimitor)]
    return param_list

  @staticmethod
  def linear_mapping(inputs, out_dim, dropout=1.0, var_scope_name="linear_mapping"):
    with tf.variable_scope(var_scope_name): 
      input_shape_tensor = tf.shape(inputs)   # dynamic shape, no None
      input_shape = inputs.get_shape().as_list()    # static shape. may has None
      assert len(input_shape) == 3
      inputs = tf.reshape(inputs, [-1, input_shape_tensor[-1]])    
      linear_mapping_w = tf.get_variable("linear_mapping_w", [input_shape[-1], out_dim], initializer=tf.random_normal_initializer(mean=0, stddev=tf.sqrt(dropout*1.0/input_shape[-1])))
      linear_mapping_b = tf.get_variable("linear_mapping_b", [out_dim], initializer=tf.constant_initializer(0.1))
      output = tf.matmul(inputs, linear_mapping_w) + linear_mapping_b
      print('xxxxx_params', input_shape, out_dim)
      #output = tf.reshape(output, [input_shape[0], -1, out_dim])
      output = tf.reshape(output, [input_shape_tensor[0], -1, out_dim])
    
      #output = tf.contrib.layers.dropout(
      #  inputs=output,
      #  keep_prob=dropout,
      #  is_training=mode == tf.contrib.learn.ModeKeys.TRAIN)
         
 
    return output
    
  @staticmethod
  def gated_linear_units(inputs):
    input_shape = inputs.get_shape().as_list()
    assert len(input_shape) == 3
    input_pass = inputs[:,:,0:int(input_shape[2]/2)]
    input_gate = inputs[:,:,int(input_shape[2]/2):]
    input_gate = tf.sigmoid(input_gate)
    return tf.multiply(input_pass, input_gate)
   
  @staticmethod
  def conv_encoder_stack(inputs, nhids_list, kwidths_list, dropout_dict, mode):
    next_layer = inputs
    for layer_idx in range(len(nhids_list)):
      nin = nhids_list[layer_idx] if layer_idx == 0 else nhids_list[layer_idx-1]
      nout = nhids_list[layer_idx]
      if nin != nout:
        #mapping for res add
        res_inputs = ConvEncoderUtils.linear_mapping(next_layer, nout, dropout=dropout_dict['src'], var_scope_name="linear_mapping_cnn_" + str(layer_idx))    
      else:
        res_inputs = next_layer
      #dropout before input to conv
      next_layer = tf.contrib.layers.dropout(
        inputs=next_layer,
        keep_prob=dropout_dict['hid'],
        is_training=mode == tf.contrib.learn.ModeKeys.TRAIN)
      
      next_layer = tf.contrib.layers.conv2d(
          inputs=next_layer,
          num_outputs=nout*2,
          kernel_size=kwidths_list[layer_idx],
          padding="SAME",   #should take attention
          weights_initializer=tf.random_normal_initializer(mean=0, stddev=tf.sqrt(4 * dropout_dict['hid'] / (kwidths_list[layer_idx] * next_layer.get_shape().as_list()[-1]))),
          biases_initializer=tf.constant_initializer(0.1),
          activation_fn=None)
      next_layer = ConvEncoderUtils.gated_linear_units(next_layer)
      next_layer = (next_layer + res_inputs) * tf.sqrt(0.5)
  
    return next_layer 


  @staticmethod
  def conv_decoder_stack(target_embed, enc_output, inputs, nhids_list, kwidths_list, dropout_dict, mode):
    next_layer = inputs
    for layer_idx in range(len(nhids_list)):
      nin = nhids_list[layer_idx] if layer_idx == 0 else nhids_list[layer_idx-1]
      nout = nhids_list[layer_idx]
      if nin != nout:
        #mapping for res add
        res_inputs = ConvEncoderUtils.linear_mapping(next_layer, nout, dropout=dropout_dict['hid'], var_scope_name="linear_mapping_cnn_" + str(layer_idx))      
      else:
        res_inputs = next_layer
      #dropout before input to conv
      next_layer = tf.contrib.layers.dropout(
        inputs=next_layer,
        keep_prob=dropout_dict['hid'],
        is_training=mode == tf.contrib.learn.ModeKeys.TRAIN)
      # special process here, first padd then conv, because tf does not suport padding other than SAME and VALID
      next_layer = tf.pad(next_layer, [[0, 0], [kwidths_list[layer_idx]-1, kwidths_list[layer_idx]-1], [0, 0]], "CONSTANT")
      next_layer = tf.contrib.layers.conv2d(
          inputs=next_layer,
          num_outputs=nout*2,
          kernel_size=kwidths_list[layer_idx],
          padding="VALID",   #should take attention, not SAME but VALID
          weights_initializer=tf.random_normal_initializer(mean=0, stddev=tf.sqrt(4 * dropout_dict['hid'] / (kwidths_list[layer_idx] * next_layer.get_shape().as_list()[-1]))),
          biases_initializer=tf.constant_initializer(0.1),
          activation_fn=None)
      
      layer_shape = next_layer.get_shape().as_list()
      assert len(layer_shape) == 3
      # to avoid using future information 
      next_layer = next_layer[:,0:-kwidths_list[layer_idx]+1,:]

      next_layer = ConvEncoderUtils.gated_linear_units(next_layer)
     
      # add attention
      # decoder output -->linear mapping to embed, + target embed,  query decoder output a, softmax --> scores, scores*encoder_output_c-->output,  output--> linear mapping to nhid+  decoder_output -->
      att_out = ConvEncoderUtils.make_attention(target_embed, enc_output, next_layer, layer_idx) 
      next_layer = (next_layer + att_out) * tf.sqrt(0.5) 

      # add res connections
      next_layer += (next_layer + res_inputs) * tf.sqrt(0.5) 
    return next_layer

  @staticmethod 
  def make_attention(target_embed, encoder_output, decoder_hidden, layer_idx):
    with tf.variable_scope("attention_layer_" + str(layer_idx)):
      embed_size = target_embed.get_shape().as_list()[-1]      #k
      dec_hidden_proj = ConvEncoderUtils.linear_mapping(decoder_hidden, embed_size, var_scope_name="linear_mapping_att_query")  # M*N1*k1 --> M*N1*k
      dec_rep = (dec_hidden_proj + target_embed) * tf.sqrt(0.5)
   
      encoder_output_a = encoder_output.outputs
      encoder_output_c = encoder_output.attention_values    # M*N2*K
      att_score = tf.matmul(dec_rep, encoder_output_a, transpose_b=True)  #M*N1*K  ** M*N2*K  --> M*N1*N2
      att_score = tf.nn.softmax(att_score)        
    
      length = tf.cast(tf.shape(encoder_output_c), tf.float32)
      att_out = tf.matmul(att_score, encoder_output_c) * length[1] * tf.sqrt(1.0/length[1])    #M*N1*N2  ** M*N2*K   --> M*N1*k
       
      att_out = ConvEncoderUtils.linear_mapping(att_out, decoder_hidden.get_shape().as_list()[-1], var_scope_name="linear_mapping_att_out")
    return att_out


