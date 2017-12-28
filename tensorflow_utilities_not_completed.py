#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Nov  1 13:57:44 2017

@author: Tao Su
"""
import numpy as np
import tensorflow as tf
import matplotlib.pyplot as plt
import networkx as nx


def batch_norm(x, is_training, axes, decay=0.99, epsilon=1e-3,scope='bn', reuse=None):
    """
    Performs a batch normalization layer. For so-called "global normalization",
    used with convolutional filters with shape [batch, height, width, depth],
    pass axes=[0, 1, 2].For simple batch normalization pass axes=[0] (batch only).

    Parameters:
        x: Input tensor.
        is_training: tf.bool type value, tensor or variable.
        axes: Array of ints. Axes along which to compute mean and variance.
        decay: The moving average decay.
        epsilon: The variance epsilon - a small float number to avoid dividing by 0.
        scope: Scope name.
        reuse:if True, we go into reuse mode for this scope as well as all sub-scopes; 
        if None, we just inherit the parent scope reuse.

    Returns:
        Batch normalization layer or maps.
    """

    with tf.variable_scope(scope,reuse=reuse):

#         beta = tf.Variable(tf.constant(0.0, shape=[x.get_shape()[-1]]),name='beta', trainable=True)
#         gamma = tf.Variable(tf.constant(1.0, shape=[x.get_shape()[-1]]),name='gamma', trainable=True)
        beta = tf.get_variable("beta", x.get_shape()[-1], initializer=tf.constant_initializer(0.0), trainable=True)
        gamma = tf.get_variable("gamma", x.get_shape()[-1], initializer=tf.constant_initializer(1.0), trainable=True)
        batch_mean, batch_var = tf.nn.moments(x, axes, name='moments')
        ema = tf.train.ExponentialMovingAverage(decay=decay)
        
        def mean_var_with_update():
            ema_apply_op = ema.apply([batch_mean, batch_var])
            with tf.control_dependencies([ema_apply_op]):
                return tf.identity(batch_mean), tf.identity(batch_var)

        mean, var = tf.cond(is_training,
                            mean_var_with_update,
                            lambda: (ema.average(batch_mean), ema.average(batch_var)))
        normed = tf.nn.batch_normalization(x, mean, var, beta, gamma, epsilon)
    return normed



#Haven't tested yet.
"""adapted from https://github.com/OlavHN/bnlstm to store separate population statistics per state"""

RNNCell = tf.nn.rnn_cell.RNNCell

class BNLSTMCell(RNNCell):
    '''Batch normalized LSTM as described in arxiv.org/abs/1603.09025'''
    def __init__(self, num_units, is_training_tensor, max_bn_steps, initial_scale=0.1, activation=tf.tanh, decay=0.95):
        """
        * max bn steps is the maximum number of steps for which to store separate population stats
        """
        self._num_units = num_units
        self._training = is_training_tensor
        self._max_bn_steps = max_bn_steps
        self._activation = activation
        self._decay = decay
        self._initial_scale = 0.1

    @property
    def state_size(self):
        return (self._num_units, self._num_units, 1)

    @property
    def output_size(self):
        return self._num_units

    def _batch_norm(self, x, name_scope, step, epsilon=1e-5, no_offset=False, set_forget_gate_bias=False):
        '''Assume 2d [batch, values] tensor'''

        with tf.variable_scope(name_scope):
            size = x.get_shape().as_list()[1]

            scale = tf.get_variable('scale', [size], initializer=tf.constant_initializer(self._initial_scale))
            if no_offset:
                offset = 0
            elif set_forget_gate_bias:
                offset = tf.get_variable('offset', [size], initializer=offset_initializer())
            else:
                offset = tf.get_variable('offset', [size], initializer=tf.zeros_initializer)

            pop_mean_all_steps = tf.get_variable('pop_mean', [self._max_bn_steps, size], initializer=tf.zeros_initializer, trainable=False)
            pop_var_all_steps = tf.get_variable('pop_var', [self._max_bn_steps, size], initializer=tf.ones_initializer(), trainable=False)

            step = tf.minimum(step, self._max_bn_steps - 1)

            pop_mean = pop_mean_all_steps[step]
            pop_var = pop_var_all_steps[step]

            batch_mean, batch_var = tf.nn.moments(x, [0])

            def batch_statistics():
                pop_mean_new = pop_mean * self._decay + batch_mean * (1 - self._decay)
                pop_var_new = pop_var * self._decay + batch_var * (1 - self._decay)
                with tf.control_dependencies([pop_mean.assign(pop_mean_new), pop_var.assign(pop_var_new)]):
                    return tf.nn.batch_normalization(x, batch_mean, batch_var, offset, scale, epsilon)

            def population_statistics():
                return tf.nn.batch_normalization(x, pop_mean, pop_var, offset, scale, epsilon)

            return tf.cond(self._training, batch_statistics, population_statistics)

    def __call__(self, x, state, scope=None):
        with tf.variable_scope(scope or type(self).__name__):
            c, h, step = state
            _step = tf.squeeze(tf.gather(tf.cast(step, tf.int32), 0))

            x_size = x.get_shape().as_list()[1]
            W_xh = tf.get_variable('W_xh',
                [x_size, 4 * self._num_units],
                initializer=orthogonal_lstm_initializer())
            W_hh = tf.get_variable('W_hh',
                [self._num_units, 4 * self._num_units],
                initializer=orthogonal_lstm_initializer())

            hh = tf.matmul(h, W_hh)
            xh = tf.matmul(x, W_xh)

            bn_hh = self._batch_norm(hh, 'hh', _step, set_forget_gate_bias=True)
            bn_xh = self._batch_norm(xh, 'xh', _step, no_offset=True)

            hidden = bn_xh + bn_hh

            f, i, o, j = tf.split(1, 4, hidden)

            new_c = c * tf.sigmoid(f) + tf.sigmoid(i) * self._activation(j)
            bn_new_c = self._batch_norm(new_c, 'c', _step)

            new_h = self._activation(bn_new_c) * tf.sigmoid(o)
            return new_h, (new_c, new_h, step+1)

def orthogonal_lstm_initializer():
    def orthogonal(shape, dtype=tf.float32, partition_info=None):
        # taken from https://github.com/cooijmanstim/recurrent-batch-normalization
        # taken from https://gist.github.com/kastnerkyle/f7464d98fe8ca14f2a1a
        """ benanne lasagne ortho init (faster than qr approach)"""
        flat_shape = (shape[0], np.prod(shape[1:]))
        a = np.random.normal(0.0, 1.0, flat_shape)
        u, _, v = np.linalg.svd(a, full_matrices=False)
        q = u if u.shape == flat_shape else v  # pick the one with the correct shape
        q = q.reshape(shape)
        return tf.constant(q[:shape[0], :shape[1]], dtype)
    return orthogonal

def offset_initializer():
    def _initializer(shape, dtype=tf.float32, partition_info=None):
        size = shape[0]
        assert size % 4 == 0
        size = size // 4
        res = [np.ones((size)), np.zeros((size*3))]
        return tf.constant(np.concatenate(res, axis=0), dtype)
    return _initializer




#Useful codes snippets
def children(op):
  return set(op for out in op.outputs for op in out.consumers())

def get_graph():
  """Creates dictionary {node: {child1, child2, ..},..} for current
  TensorFlow graph. Result is compatible with networkx/toposort"""

  ops = tf.get_default_graph().get_operations()
  return {op: children(op) for op in ops}

def plot_graph(G):
    '''Plot a DAG using NetworkX'''        
    def mapping(node):
        return node.name
    G = nx.DiGraph(G)
    nx.relabel_nodes(G, mapping, copy=False)
    nx.draw(G, cmap = plt.get_cmap('jet'), with_labels = True)
    plt.show()


#x = tf.Variable(0, name='x')
#model = tf.global_variables_initializer()
#with tf.Session() as session:
#    for i in range(5):
#        session.run(model)
#        x = x + 1
#        print(session.run(x))
#
#        plot_graph(get_graph())