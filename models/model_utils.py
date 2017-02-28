import math
import tensorflow as tf

def variable_summaries(var, groupname, name):
    """Attach a lot of summaries to a Tensor.
        This is also quite expensive.
    """
    with tf.device("/cpu:0"), tf.name_scope(None):
        s_var = tf.cast(var, tf.float32)
        amean = tf.reduce_mean(tf.abs(s_var))
        tf.summary.scalar(groupname + '/amean/' + name, amean)
        mean = tf.reduce_mean(s_var)
        tf.summary.scalar(groupname + '/mean/' + name, mean)
        stddev = tf.sqrt(tf.reduce_sum(tf.square(s_var - mean)))
        tf.summary.scalar(groupname + '/sttdev/' + name, stddev)
        tf.summary.scalar(groupname + '/max/' + name, tf.reduce_max(s_var))
        tf.summary.scalar(groupname + '/min/' + name, tf.reduce_min(s_var))
        tf.summary.histogram(groupname + "/" + name, s_var)

def getdtype(hps, is_rnn=False):
    if is_rnn:
        return tf.float16 if hps.float16_rnn else tf.float32
    else:
        return tf.float16 if hps.float16_non_rnn else tf.float32


def linear(x, size, name):
    w = tf.get_variable(name + "/W", [x.get_shape()[-1], size])
    b = tf.get_variable(name + "/b", [1, size], initializer=tf.zeros_initializer)
    return tf.matmul(x, w) + b


def sharded_variable(name, shape, num_shards, dtype=tf.float32, transposed=False):
    # The final size of the sharded variable may be larger than requested.
    # This should be fine for embeddings.
    shard_size = int((shape[0] + num_shards - 1) / num_shards)
    if transposed:
        initializer = tf.uniform_unit_scaling_initializer(dtype=dtype)
    else:
        initializer = tf.uniform_unit_scaling_initializer(dtype=dtype)
    return [tf.get_variable(name + "_" + str(i), [shard_size, shape[1]],
                            initializer=initializer, dtype=dtype) for i in range(num_shards)]


# XXX(rafal): Code below copied from rnn_cell.py
def _get_sharded_variable(name, shape, dtype, num_shards):
    """Get a list of sharded variables with the given dtype."""
    if num_shards > shape[0]:
        raise ValueError("Too many shards: shape=%s, num_shards=%d" %
                         (shape, num_shards))
    unit_shard_size = int(math.floor(shape[0] / num_shards))
    remaining_rows = shape[0] - unit_shard_size * num_shards

    shards = []
    for i in range(num_shards):
        current_size = unit_shard_size
        if i < remaining_rows:
            current_size += 1
        shards.append(tf.get_variable(name + "_%d" % i, [current_size] + shape[1:], dtype=dtype))
    return shards


def _get_concat_variable(name, shape, dtype, num_shards):
    """Get a sharded variable concatenated into one tensor."""
    _sharded_variable = _get_sharded_variable(name, shape, dtype, num_shards)
    if len(_sharded_variable) == 1:
        return _sharded_variable[0]

    return tf.concat(_sharded_variable, 0)


class FLSTMCell(tf.contrib.rnn.RNNCell):
    """LSTMCell with factorized matrix"""
    def __init__(self, num_units, input_size, initializer=None,
                 num_proj=None, num_shards=1, factor_size=None, fnon_linearity=None, dtype=tf.float32):
        self._num_units = num_units
        self._initializer = initializer
        self._num_proj = num_proj
        self._num_unit_shards = num_shards
        self._num_proj_shards = num_shards
        self._forget_bias = 1.0
        if factor_size:
            self._factor_size = int(factor_size)
        else:
            self._factor_size = None
        self._fnon_linearity = fnon_linearity

        if num_proj:
            self._state_size = num_units + num_proj
            self._output_size = num_proj
        else:
            self._state_size = 2 * num_units
            self._output_size = num_units

        with tf.variable_scope("LSTMCell"):
            if self._factor_size:
                self._concat_w1 = _get_concat_variable(
                    "W1", [input_size + num_proj, self._factor_size],
                    dtype, self._num_unit_shards)
                self._concat_w2 = _get_concat_variable(
                    "W2", [self._factor_size, 4 * self._num_units],
                    dtype, self._num_unit_shards)
                if self._fnon_linearity:
                    self._b1 = tf.get_variable(name="b1", shape = [self._factor_size])
            else:
                self._concat_w = _get_concat_variable(
                    "W", [input_size + num_proj, 4 * self._num_units],
                    dtype, self._num_unit_shards)

            self._b = tf.get_variable(
                "B", shape=[4 * self._num_units])

            self._concat_w_proj = _get_concat_variable(
                "W_P", [self._num_units, self._num_proj],
                dtype, self._num_proj_shards)

    @property
    def state_size(self):
        return self._state_size

    @property
    def output_size(self):
        return self._output_size

    def __call__(self, inputs, state, scope=None):
        num_proj = self._num_units if self._num_proj is None else self._num_proj

        c_prev = tf.slice(state, [0, 0], [-1, self._num_units])
        m_prev = tf.slice(state, [0, self._num_units], [-1, num_proj])

        input_size = inputs.get_shape().with_rank(2)[1]
        if input_size.value is None:
            raise ValueError("Could not infer input size from inputs.get_shape()[-1]")
        with tf.variable_scope(type(self).__name__,
                               initializer=self._initializer):  # "LSTMCell"
            # i = input_gate, j = new_input, f = forget_gate, o = output_gate
            cell_inputs = tf.concat([inputs, m_prev], 1)
            if self._factor_size:
                if self._fnon_linearity:
                    lstm_matrix = tf.nn.bias_add(tf.matmul(
                        self._fnon_linearity(tf.nn.bias_add(tf.matmul(cell_inputs, self._concat_w1),self._b1)),
                        self._concat_w2), self._b)
                else:
                    lstm_matrix = tf.nn.bias_add(tf.matmul(tf.matmul(cell_inputs, self._concat_w1), self._concat_w2), self._b)
            else:
                lstm_matrix = tf.matmul(cell_inputs, self._concat_w) + self._b

            i, j, f, o = tf.split(lstm_matrix, 4, 1)

            c = tf.sigmoid(f + 1.0) * c_prev + tf.sigmoid(i) * tf.tanh(j)
            m = tf.sigmoid(o) * tf.tanh(c)

            if self._num_proj is not None:
                m = tf.matmul(m, self._concat_w_proj)

        new_state = tf.concat([c, m], 1)
        return m, new_state


class GLSTMCell(tf.contrib.rnn.RNNCell):
    """LSTM cell with groups"""
    def __init__(self, num_units, input_size, initializer=None,
                 num_proj=None, num_shards=1, number_of_groups=1, dtype=tf.float32):

        self._num_units = num_units
        self._initializer = initializer
        self._num_proj = num_proj
        self._num_unit_shards = num_shards
        self._num_proj_shards = num_shards
        self._forget_bias = 1.0
        self._number_of_groups = number_of_groups

        #currently we only support input and projection of the same size
        assert(input_size == self._num_proj)
        assert(input_size % self._number_of_groups == 0)
        assert(self._num_units % self._number_of_groups == 0)
        self._group_shape = [input_size / self._number_of_groups,
                             self._num_units / self._number_of_groups]
        print('LSTM cell group shape: ' + str(self._group_shape))

        if num_proj:
            self._state_size = num_units + num_proj
            self._output_size = num_proj
        else:
            self._state_size = 2 * num_units
            self._output_size = num_units

        with tf.variable_scope("LSTMCell"):
            self._Wks = []
            for group_id in xrange(self._number_of_groups):
                #adding group matrix reponsible for input part and
                #group matrix responsible for state part of the input to glstm cell

                #we fuse i, j, f, o gates, hence 4*self._group_shape[1], we also fuse inpt and state, hence 2*self._group_shape[0]
                self._Wks.append(_get_concat_variable("W_" + str(group_id), [2*self._group_shape[0], 4*self._group_shape[1]], dtype, self._num_proj_shards))

            #biases for gates
            self._b_i = tf.get_variable(
                "B_i", shape=[self._num_units])
            self._b_j = tf.get_variable(
                "B_j", shape=[self._num_units])
            self._b_f = tf.get_variable(
                "B_f", shape=[self._num_units])
            self._b_o = tf.get_variable(
                "B_o", shape=[self._num_units])

            #projection matrix
            self._concat_w_proj = _get_concat_variable(
                "W_P", [self._num_units, self._num_proj],
                dtype, self._num_proj_shards)

    @property
    def state_size(self):
        return self._state_size

    @property
    def output_size(self):
        return self._output_size

    def _get_input_for_group(self, inpt, group_id, group_size):
        return tf.slice(inpt, [0, group_id*group_size], [inpt.get_shape()[0].value, group_size])

    def __call__(self, inputs, state, scope=None):
        num_proj = self._num_units if self._num_proj is None else self._num_proj

        c_prev = tf.slice(state, [0, 0], [-1, self._num_units])
        m_prev = tf.slice(state, [0, self._num_units], [-1, num_proj])

        input_size = inputs.get_shape().with_rank(2)[1]
        if input_size.value is None:
            raise ValueError("Could not infer input size from inputs.get_shape()[-1]")
        with tf.variable_scope(type(self).__name__,
                               initializer=self._initializer):  # "LSTMCell"
            # i = input_gate, j = new_input, f = forget_gate, o = output_gate
            i_parts = []
            j_parts = []
            f_parts = []
            o_parts = []

            for group_id in xrange(self._number_of_groups):
                x_g_id = tf.concat([self._get_input_for_group(inputs, group_id, self._group_shape[0]),
                self._get_input_for_group(m_prev, group_id, self._group_shape[0])], axis =1)

                R_k = tf.matmul(x_g_id, self._Wks[group_id], name="R_"+str(group_id))
                i_k, j_k, f_k, o_k = tf.split(R_k, 4, 1)
                i_parts.append(i_k)
                j_parts.append(j_k)
                f_parts.append(f_k)
                o_parts.append(o_k)

            i = tf.nn.bias_add(tf.concat(i_parts, axis=1), self._b_i)
            j = tf.nn.bias_add(tf.concat(j_parts, axis=1), self._b_j)
            f = tf.nn.bias_add(tf.concat(f_parts, axis=1), self._b_f)
            o = tf.nn.bias_add(tf.concat(o_parts, axis=1), self._b_o)

            c = tf.sigmoid(f + 1.0) * c_prev + tf.sigmoid(i) * tf.tanh(j)
            m = tf.sigmoid(o) * tf.tanh(c)

            if self._num_proj is not None:
                m = tf.matmul(m, self._concat_w_proj)

        new_state = tf.concat([c, m], 1)
        return m, new_state
