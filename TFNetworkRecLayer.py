
from __future__ import print_function

import tensorflow as tf
from TFNetworkLayer import LayerBase, _ConcatInputLayer, SearchChoices, get_concat_sources_data_template
from TFUtil import Data, reuse_name_scope
from Log import log


class RecLayer(_ConcatInputLayer):
  """
  Recurrent layer, has support for several implementations of LSTMs (via ``unit`` argument),
  see :ref:`tf_lstm_benchmark` (http://returnn.readthedocs.io/en/latest/tf_lstm_benchmark.html),
  and also GRU, or simple RNN.

  A subnetwork can also be given which will be evaluated step-by-step,
  which can use attention over some separate input,
  which can be used to implement a decoder in a sequence-to-sequence scenario.
  """

  layer_class = "rec"
  recurrent = True

  def __init__(self,
               unit="lstm",
               direction=None, input_projection=True,
               initial_state=None,
               max_seq_len=None,
               forward_weights_init=None, recurrent_weights_init=None, bias_init=None,
               **kwargs):
    """
    :param str|dict[str,dict[str]] unit: the RNNCell/etc name, e.g. "nativelstm". see comment below.
      alternatively a whole subnetwork, which will be executed step by step,
      and which can include "prev" in addition to "from" to refer to previous steps.
    :param int|None direction: None|1 -> forward, -1 -> backward
    :param bool input_projection: True -> input is multiplied with matrix. False only works if same input dim
    :param LayerBase|None initial_state:
    :param int max_seq_len: if unit is a subnetwork
    :param str forward_weights_init: see :func:`TFUtil.get_initializer`
    :param str recurrent_weights_init: see :func:`TFUtil.get_initializer`
    :param str bias_init: see :func:`TFUtil.get_initializer`
    """
    super(RecLayer, self).__init__(**kwargs)
    from TFUtil import is_gpu_available
    from tensorflow.contrib import rnn as rnn_contrib
    if is_gpu_available():
      from tensorflow.contrib import cudnn_rnn
    else:
      cudnn_rnn = None
    import TFNativeOp
    if direction is not None:
      assert direction in [-1, 1]
    self._last_hidden_state = None
    self._direction = direction
    self._initial_state_src = initial_state
    self._initial_state = initial_state.get_last_hidden_state() if initial_state else None
    self._input_projection = input_projection
    self._max_seq_len = max_seq_len
    self._sub_loss = None
    self._sub_error = None
    self._sub_loss_normalization_factor = None
    # On the random initialization:
    # For many cells, e.g. NativeLSTM: there will be a single recurrent weight matrix, (output.dim, output.dim * 4),
    # and a single input weight matrix (input_data.dim, output.dim * 4), and a single bias (output.dim * 4,).
    # The bias is by default initialized with 0.
    # In the Theano :class:`RecurrentUnitLayer`, create_recurrent_weights() and create_forward_weights() are used,
    #   where forward_weights_init = "random_uniform(p_add=%i)" % (output.dim * 4)
    #   and recurrent_weights_init = "random_uniform()",
    #   thus with in=input_data.dim, out=output.dim,
    #   for forward weights: uniform sqrt(6. / (in + out*8)), for rec. weights: uniform sqrt(6. / (out*5)).
    # TensorFlow initializers:
    #   https://www.tensorflow.org/api_guides/python/contrib.layers#Initializers
    #   https://www.tensorflow.org/api_docs/python/tf/orthogonal_initializer
    #   https://github.com/tensorflow/tensorflow/blob/master/tensorflow/python/ops/init_ops.py
    #   xavier_initializer with uniform=True: uniform sqrt(6 / (fan_in + fan_out)),
    #     i.e. uniform sqrt(6. / (in + out*4)) for forward, sqrt(6./(out*5)) for rec.
    #     Ref: https://www.tensorflow.org/api_docs/python/tf/contrib/layers/xavier_initializer
    # Keras uses these defaults:
    #   Ref: https://github.com/fchollet/keras/blob/master/keras/layers/recurrent.py
    #   Ref: https://keras.io/initializers/, https://github.com/fchollet/keras/blob/master/keras/engine/topology.py
    #   (fwd weights) kernel_initializer='glorot_uniform', recurrent_initializer='orthogonal',
    #   where glorot_uniform is sqrt(6 / (fan_in + fan_out)), i.e. fwd weights: uniform sqrt(6 / (in + out*4)),
    #   and orthogonal creates a random orthogonal matrix (fan_in, fan_out), i.e. rec (out, out*4).
    self._bias_initializer = tf.constant_initializer(0.0)
    self._fwd_weights_initializer = None
    self._rec_weights_initializer = None
    from TFUtil import get_initializer, xavier_initializer
    if forward_weights_init:
      self._fwd_weights_initializer = get_initializer(
        forward_weights_init, seed=self.network.random.randint(2**31), eval_local_ns={"layer": self})
    if recurrent_weights_init:
      self._rec_weights_initializer = get_initializer(
        recurrent_weights_init, seed=self.network.random.randint(2**31), eval_local_ns={"layer": self})
    if bias_init:
      self._bias_initializer = get_initializer(
        bias_init, seed=self.network.random.randint(2**31), eval_local_ns={"layer": self})
    with tf.variable_scope(
          "rec",
          initializer=xavier_initializer(seed=self.network.random.randint(2**31))) as scope:
      assert isinstance(scope, tf.VariableScope)
      self._rec_scope = scope
      scope_name_prefix = scope.name + "/"  # e.g. "layer1/rec/"
      with self.var_creation_scope():
        self.cell = self._get_cell(unit)
        if isinstance(self.cell, (rnn_contrib.RNNCell, rnn_contrib.FusedRNNCell)):
          y = self._get_output_cell(self.cell)
        elif cudnn_rnn and isinstance(self.cell, (cudnn_rnn.CudnnLSTM, cudnn_rnn.CudnnGRU)):
          y = self._get_output_cudnn(self.cell)
        elif isinstance(self.cell, TFNativeOp.RecSeqCellOp):
          y = self._get_output_native_rec_op(self.cell)
        elif isinstance(self.cell, _SubnetworkRecCell):
          y = self._get_output_subnet_unit(self.cell)
        else:
          raise Exception("invalid type: %s" % type(self.cell))
        self.output.time_dim_axis = 0
        self.output.batch_dim_axis = 1
        self.output.placeholder = y
        params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope_name_prefix)
        self.params.update({p.name[len(scope_name_prefix):-2]: p for p in params})

  def get_dep_layers(self):
    l = super(RecLayer, self).get_dep_layers()
    if self._initial_state_src:
      l += [self._initial_state_src]
    if isinstance(self.cell, _SubnetworkRecCell):
      l += self.cell.get_parent_deps()
    return l

  @classmethod
  def transform_config_dict(cls, d, network, get_layer):
    """
    :param dict[str] d: will modify inplace
    :param TFNetwork.TFNetwork network:
    :param ((str) -> LayerBase) get_layer: function to get or construct another layer
    """
    if isinstance(d.get("unit"), dict):
      d["n_out"] = d.get("n_out", None)  # disable automatic guessing
    super(RecLayer, cls).transform_config_dict(d, network=network, get_layer=get_layer)
    initial_state = d.pop("initial_state", None)
    if initial_state:
      d["initial_state"] = get_layer(initial_state)
    if isinstance(d.get("unit"), dict):
      def sub_get_layer(name):
        # Only used to resolve deps to base network.
        if name.startswith("base:"):
          return get_layer(name[len("base:"):])
      for sub in d["unit"].values():  # iterate over the layers of the subnet
        assert isinstance(sub, dict)
        if "class" in sub:
          from TFNetworkLayer import get_layer_class
          class_name = sub["class"]
          cl = get_layer_class(class_name)
          # Operate on a copy because we will transform the dict later.
          # We only need this to resolve any other layer dependencies in the main network.
          cl.transform_config_dict(sub.copy(), network=network, get_layer=sub_get_layer)

  @classmethod
  def get_out_data_from_opts(cls, unit, sources=(), initial_state=None, **kwargs):
    n_out = kwargs.get("n_out", None)
    out_type = kwargs.get("out_type", None)
    loss = kwargs.get("loss", None)
    deps = list(sources)  # type: list[LayerBase]
    if initial_state:
      deps += [initial_state]
    if out_type or n_out or loss:
      if out_type:
        assert out_type.get("time_dim_axis", 0) == 0
        assert out_type.get("batch_dim_axis", 1) == 1
      out = super(RecLayer, cls).get_out_data_from_opts(**kwargs)
    else:
      out = None
    if isinstance(unit, dict):  # subnetwork
      source_data = get_concat_sources_data_template(sources) if sources else None
      subnet = _SubnetworkRecCell(parent_net=kwargs["network"], net_dict=unit, source_data=source_data)
      sub_out = subnet.layer_data_templates["output"].output.copy_template_adding_time_dim(
        name="%s_output" % kwargs["name"], time_dim_axis=0)
      if out:
        assert sub_out.dim == out.dim
        assert sub_out.shape == out.shape
      out = sub_out
      deps += subnet.get_parent_deps()
    assert out
    out.time_dim_axis = 0
    out.batch_dim_axis = 1
    cls._post_init_output(output=out, sources=sources, **kwargs)
    for dep in deps:
      out.beam_size = out.beam_size or dep.output.beam_size
    return out

  def get_absolute_name_scope_prefix(self):
    return self.get_base_absolute_name_scope_prefix() + "rec/"  # all under "rec" sub-name-scope

  _rnn_cells_dict = {}

  @classmethod
  def _create_rnn_cells_dict(cls):
    from TFUtil import is_gpu_available
    from tensorflow.contrib import rnn as rnn_contrib
    import TFNativeOp
    allowed_types = (rnn_contrib.RNNCell, rnn_contrib.FusedRNNCell, TFNativeOp.RecSeqCellOp)
    if is_gpu_available():
      from tensorflow.contrib import cudnn_rnn
      allowed_types += (cudnn_rnn.CudnnLSTM, cudnn_rnn.CudnnGRU)
    else:
      cudnn_rnn = None
    def maybe_add(key, v):
      if isinstance(v, type) and issubclass(v, allowed_types):
        name = key
        if name.endswith("Cell"):
          name = name[:-len("Cell")]
        name = name.lower()
        assert cls._rnn_cells_dict.get(name) in [v, None]
        cls._rnn_cells_dict[name] = v
    for key, v in vars(rnn_contrib).items():
      maybe_add(key, v)
    for key, v in vars(TFNativeOp).items():
      maybe_add(key, v)
    if is_gpu_available():
      for key, v in vars(cudnn_rnn).items():
        maybe_add(key, v)
    # Alias for the standard LSTM cell, because self._get_cell(unit="lstm") will use "NativeLSTM" by default.
    maybe_add("StandardLSTM", rnn_contrib.LSTMCell)

  _warn_msg_once_for_cell_name = set()

  @classmethod
  def get_rnn_cell_class(cls, name):
    """
    :param str name: cell name, minus the "Cell" at the end
    :rtype: () -> tensorflow.contrib.rnn.RNNCell
    """
    if not cls._rnn_cells_dict:
      cls._create_rnn_cells_dict()
    from TFUtil import is_gpu_available
    if not is_gpu_available():
      m = {"cudnnlstm": "LSTMBlockFused", "cudnngru": "GRUBlock"}
      if name.lower() in m:
        if name.lower() not in cls._warn_msg_once_for_cell_name:
          print("You have selected unit %r in a rec layer which is for GPU only, so we are using %r instead." %
                (name, m[name.lower()]), file=log.v2)
          cls._warn_msg_once_for_cell_name.add(name.lower())
        name = m[name.lower()]
    return cls._rnn_cells_dict[name.lower()]

  def _get_input(self):
    """
    :return: (x, seq_len), where x is (time,batch,...,dim) and seq_len is (batch,)
    :rtype: (tf.Tensor, tf.Tensor)
    """
    assert self.input_data
    x = self.input_data.placeholder  # (batch,time,dim) or (time,batch,dim)
    if not self.input_data.is_time_major:
      assert self.input_data.batch_dim_axis == 0
      assert self.input_data.time_dim_axis == 1
      x = self.input_data.get_placeholder_as_time_major()  # (time,batch,[dim])
    seq_len = self.input_data.size_placeholder[0]
    return x, seq_len

  def get_loss_value(self):
    v = super(RecLayer, self).get_loss_value()
    from TFUtil import optional_add
    return optional_add(v, self._sub_loss)

  def get_error_value(self):
    v = super(RecLayer, self).get_error_value()
    if v is not None:
      return v
    return self._sub_error

  def get_loss_normalization_factor(self):
    v = super(RecLayer, self).get_loss_normalization_factor()
    if v is not None:
      return v
    return self._sub_loss_normalization_factor

  def get_constraints_value(self):
    v = super(RecLayer, self).get_constraints_value()
    from TFUtil import optional_add
    if isinstance(self.cell, _SubnetworkRecCell):
      for layer in self.cell.net.layers.values():
        v = optional_add(v, layer.get_constraints_value())
    return v

  def _get_cell(self, unit):
    """
    :param str|dict[str] unit:
    :rtype: RecLayer.SubnetworkCell|tensorflow.contrib.rnn.RNNCell|tensorflow.contrib.rnn.FusedRNNCell|TFNativeOp.RecSeqCellOp
    """
    from TFUtil import is_gpu_available
    from tensorflow.contrib import rnn as rnn_contrib
    import TFNativeOp
    if isinstance(unit, dict):
      return _SubnetworkRecCell(parent_rec_layer=self, net_dict=unit)
    assert isinstance(unit, str)
    if unit.lower() in ["lstmp", "lstm"]:
      # Some possible LSTM implementations are (in all cases for both CPU and GPU):
      # * BasicLSTM (the cell), via official TF, pure TF implementation
      # * LSTMBlock (the cell), via tf.contrib.rnn.
      # * LSTMBlockFused, via tf.contrib.rnn. should be much faster than BasicLSTM
      # * NativeLSTM, our own native LSTM. should be faster than LSTMBlockFused
      # * CudnnLSTM, via tf.contrib.cudnn_rnn. This is experimental yet.
      # We default to the current tested fastest one, i.e. NativeLSTM.
      # Note that they are currently not compatible to each other, i.e. the way the parameters are represented.
      unit = "nativelstm"  # TFNativeOp.NativeLstmCell
    rnn_cell_class = self.get_rnn_cell_class(unit)
    n_hidden = self.output.dim
    if is_gpu_available():
      from tensorflow.contrib import cudnn_rnn
      if issubclass(rnn_cell_class, (cudnn_rnn.CudnnLSTM, cudnn_rnn.CudnnGRU)):
        cell = rnn_cell_class(
          num_layers=1, num_units=n_hidden, input_size=self.input_data.dim,
          input_mode='linear_input', direction='unidirectional', dropout=0.0)
        return cell
    if issubclass(rnn_cell_class, TFNativeOp.RecSeqCellOp):
      cell = rnn_cell_class(
        n_hidden=n_hidden, n_input_dim=self.input_data.dim,
        input_is_sparse=self.input_data.sparse,
        step=self._direction)
      return cell
    cell = rnn_cell_class(n_hidden)
    assert isinstance(
      cell, (rnn_contrib.RNNCell, rnn_contrib.FusedRNNCell))  # e.g. BasicLSTMCell
    return cell

  def _get_output_cell(self, cell):
    """
    :param tensorflow.contrib.rnn.RNNCell|tensorflow.contrib.rnn.FusedRNNCell cell:
    :return: output of shape (time, batch, dim)
    :rtype: tf.Tensor
    """
    from tensorflow.python.ops import rnn
    from tensorflow.contrib import rnn as rnn_contrib
    assert self._max_seq_len is None
    assert self.input_data
    assert not self.input_data.sparse
    x, seq_len = self._get_input()
    if self._direction == -1:
      x = tf.reverse_sequence(x, seq_lengths=seq_len, batch_dim=1, seq_dim=0)
    if isinstance(cell, rnn_contrib.RNNCell):  # e.g. BasicLSTMCell
      # Will get (time,batch,ydim).
      y, final_state = rnn.dynamic_rnn(
        cell=cell, inputs=x, time_major=True, sequence_length=seq_len, dtype=tf.float32,
        initial_state=self._initial_state)
      self._last_hidden_state = final_state
    elif isinstance(cell, rnn_contrib.FusedRNNCell):  # e.g. LSTMBlockFusedCell
      # Will get (time,batch,ydim).
      y, final_state = cell(
        inputs=x, sequence_length=seq_len, dtype=tf.float32,
        initial_state=self._initial_state)
      self._last_hidden_state = final_state
    else:
      raise Exception("invalid type: %s" % type(cell))
    if self._direction == -1:
      y = tf.reverse_sequence(y, seq_lengths=seq_len, batch_dim=1, seq_dim=0)
    return y

  @staticmethod
  def _get_cudnn_param_size(num_units, input_size,
                            num_layers=1, rnn_mode="lstm", input_mode="linear_input", direction='unidirectional'):
    """
    :param int num_layers:
    :param int num_units:
    :param int input_size:
    :param str rnn_mode: 'lstm', 'gru', 'rnn_tanh' or 'rnn_relu'
    :param str input_mode: "linear_input", "skip_input", "auto_select". note that we have a different default.
    :param str direction: 'unidirectional' or 'bidirectional'
    :return: size
    :rtype: int
    """
    # Also see test_RecLayer_get_cudnn_params_size().
    dir_count = {"unidirectional": 1, "bidirectional": 2}[direction]
    num_gates = {"lstm": 3, "gru": 2}.get(rnn_mode, 0)
    if input_mode == "linear_input" or (input_mode == "auto_select" and num_units != input_size):
      # (input + recurrent + 2 * bias) * output * (gates + cell in)
      size = (input_size + num_units + 2) * num_units * (num_gates + 1) * dir_count
    elif input_mode == "skip_input" or (input_mode == "auto_select" and num_units == input_size):
      # (recurrent + 2 * bias) * output * (gates + cell in)
      size = (num_units + 2) * num_units * (num_gates + 1) * dir_count
    else:
      raise Exception("invalid input_mode %r" % input_mode)
    # Remaining layers:
    size += (num_units * dir_count + num_units + 2) * num_units * (num_gates + 1) * dir_count * (num_layers - 1)
    return size

  @staticmethod
  def convert_cudnn_canonical_to_lstm_block(reader, prefix, target="lstm_block_wrapper/"):
    """
    This assumes CudnnLSTM currently, with num_layers=1, input_mode="linear_input", direction='unidirectional'!

    :param tf.train.CheckpointReader reader:
    :param str prefix: e.g. "layer2/rec/"
    :param str target: e.g. "lstm_block_wrapper/" or "rnn/lstm_cell/"
    :return: dict key -> value, {".../kernel": ..., ".../bias": ...} with prefix
    :rtype: dict[str,numpy.ndarray]
    """
    # For reference:
    # https://github.com/tensorflow/tensorflow/blob/master/tensorflow/contrib/cudnn_rnn/python/ops/cudnn_rnn_ops.py
    # For CudnnLSTM, there are 8 tensors per weight and per bias for each
    # layer: tensor 0-3 are applied to the input from the previous layer and
    # tensor 4-7 to the recurrent input. Tensor 0 and 4 are for the input gate;
    # tensor 1 and 5 the forget gate; tensor 2 and 6 the new memory gate;
    # tensor 3 and 7 the output gate.
    import numpy
    num_vars = 16
    values = []
    for i in range(num_vars):
      values.append(reader.get_tensor("%scudnn/CudnnRNNParamsToCanonical:%i" % (prefix, i)))
    assert len(values[-1].shape) == 1
    output_dim = values[-1].shape[0]
    # For some reason, the input weight matrices are sometimes flattened.
    assert numpy.prod(values[0].shape) % output_dim == 0
    input_dim = numpy.prod(values[0].shape) // output_dim
    weights_and_biases = [
      (numpy.concatenate(
        [numpy.reshape(values[i], [output_dim, input_dim]),  # input weights
         numpy.reshape(values[i + 4], [output_dim, output_dim])],  # recurrent weights
        axis=1),
       values[8 + i] +  # input bias
       values[8 + i + 4]  # recurrent bias
      )
      for i in range(4)]
    # cuDNN weights are in ifco order, convert to icfo order.
    weights_and_biases[1:3] = reversed(weights_and_biases[1:3])
    weights = numpy.transpose(numpy.concatenate([wb[0] for wb in weights_and_biases], axis=0))
    biases = numpy.concatenate([wb[1] for wb in weights_and_biases], axis=0)
    return {prefix + target + "kernel": weights, prefix + target + "bias": biases}

  def _get_output_cudnn(self, cell):
    """
    :param tensorflow.contrib.cudnn_rnn.CudnnLSTM|tensorflow.contrib.cudnn_rnn.CudnnGRU cell:
    :return: output of shape (time, batch, dim)
    :rtype: tf.Tensor
    """
    from TFUtil import get_current_var_scope_name
    from tensorflow.contrib.cudnn_rnn import RNNParamsSaveable
    assert self._max_seq_len is None
    assert self.input_data
    assert not self.input_data.sparse
    x, seq_len = self._get_input()
    n_batch = tf.shape(seq_len)[0]
    if self._direction == -1:
      x = tf.reverse_sequence(x, seq_lengths=seq_len, batch_dim=1, seq_dim=0)
    with tf.variable_scope("cudnn"):
      num_layers = 1
      param_size = self._get_cudnn_param_size(
        num_units=self.output.dim, input_size=self.input_data.dim, rnn_mode=cell._rnn_mode, num_layers=num_layers)
      # Note: The raw params used during training for the cuDNN op is just a single variable
      # with all params concatenated together.
      # For the checkpoint save/restore, we will use RNNParamsSaveable, which also makes it easier in CPU mode
      # to import the params for another unit like LSTMBlockCell.
      # Also see: https://github.com/tensorflow/tensorflow/blob/master/tensorflow/contrib/cudnn_rnn/python/kernel_tests/cudnn_rnn_ops_test.py
      params = tf.Variable(
        tf.random_uniform([param_size], minval=-0.01, maxval=0.01, seed=42), name="params_raw", trainable=True)
      params_saveable = RNNParamsSaveable(
        params_to_canonical=cell.params_to_canonical,
        canonical_to_params=cell.canonical_to_params,
        param_variables=[params],
        name="%s/params_canonical" % get_current_var_scope_name())
      params_saveable.op = params
      tf.add_to_collection(tf.GraphKeys.SAVEABLE_OBJECTS, params_saveable)
      self.saveable_param_replace[params] = params_saveable
      # It's like a fused cell, i.e. operates on the full sequence.
      input_h = tf.zeros((num_layers, n_batch, self.output.dim), dtype=tf.float32)
      input_c = tf.zeros((num_layers, n_batch, self.output.dim), dtype=tf.float32)
      y, _, _ = cell(x, input_h=input_h, input_c=input_c, params=params)
    if self._direction == -1:
      y = tf.reverse_sequence(y, seq_lengths=seq_len, batch_dim=1, seq_dim=0)
    return y

  def _get_output_native_rec_op(self, cell):
    """
    :param TFNativeOp.RecSeqCellOp cell:
    :return: output of shape (time, batch, dim)
    :rtype: tf.Tensor
    """
    from TFUtil import dot, sequence_mask_time_major, directed
    assert self._max_seq_len is None
    assert self.input_data
    x, seq_len = self._get_input()
    if self._input_projection:
      if cell.does_input_projection:
        # The cell get's x as-is. It will internally does the matrix mult and add the bias.
        pass
      else:
        W = tf.get_variable(
          name="W", shape=(self.input_data.dim, cell.n_input_dim), dtype=tf.float32,
          initializer=self._fwd_weights_initializer)
        if self.input_data.sparse:
          x = tf.nn.embedding_lookup(W, x)
        else:
          x = dot(x, W)
        b = tf.get_variable(name="b", shape=(cell.n_input_dim,), dtype=tf.float32, initializer=self._bias_initializer)
        x += b
    else:
      assert not cell.does_input_projection
      assert not self.input_data.sparse
      assert self.input_data.dim == cell.n_input_dim
    index = sequence_mask_time_major(seq_len, maxlen=self.input_data.time_dimension())
    if not cell.does_direction_handling:
      x = directed(x, self._direction)
      index = directed(index, self._direction)
    y, final_state = cell(
      inputs=x, index=index,
      initial_state=self._initial_state,
      recurrent_weights_initializer=self._rec_weights_initializer)
    self._last_hidden_state = final_state
    if not cell.does_direction_handling:
      y = directed(y, self._direction)
    return y

  def _get_output_subnet_unit(self, cell):
    """
    :param _SubnetworkRecCell cell:
    :return: output of shape (time, batch, dim)
    :rtype: tf.Tensor
    """
    cell.check_output_template_shape()
    from TFUtil import check_input_dim

    with tf.name_scope("subnet_base"):
      batch_dim = self.network.get_batch_dim()
      input_beam_size = None  # type: int | None
      if self.input_data:
        with tf.name_scope("x_tensor_array"):
          x, seq_len = self._get_input()  # x will be (time,batch,..,dim)
          x_shape = tf.shape(x)
          x_ta = tf.TensorArray(
            name="x_ta",
            dtype=self.input_data.dtype,
            element_shape=tf.TensorShape(self.input_data.copy_template_excluding_time_dim().batch_shape),
            size=x_shape[0],
            infer_shape=True)
          x_ta = x_ta.unstack(x)
        input_search_choices = self.network.get_search_choices(sources=self.sources)
        if input_search_choices:
          input_beam_size = input_search_choices.search_choices.beam_size
      else:
        x_ta = None
        if self.output.size_placeholder:
          # see LayerBase._post_init_output(). could be set via target or size_target...
          seq_len = self.output.size_placeholder[0]
        else:
          seq_len = None
      if seq_len is not None:
        with tf.name_scope("check_seq_len_batch_size"):
          seq_len = check_input_dim(seq_len, axis=0, dim=batch_dim * (input_beam_size or 1))
        max_seq_len = tf.reduce_max(seq_len)
        have_known_seq_len = True
      else:
        assert self._max_seq_len, "must specify max_seq_len in rec layer"
        max_seq_len = self._max_seq_len
        have_known_seq_len = False
      # if not self.input_data and self.network.search_flag:
      #   assert not have_known_seq_len  # at least for the moment

      # TODO: Better check for train_flag.
      # Maybe more generic via sampling options later.
      y_ta = None
      if self.target and self.network.train_flag is not False:
        # TODO check subnet, which extern data keys are used...
        y_data = self.network.get_extern_data(self.target, mark_data_key_as_used=True)
        y = y_data.get_placeholder_as_time_major()
        y_max_len = tf.shape(y)[0]
        if seq_len is not None:
          with tf.control_dependencies([tf.assert_equal(max_seq_len, y_max_len,
              ["RecLayer %r with sources %r." % (self.name, self.sources),
               " The length of the sources (", max_seq_len,
               ") differ from the length of the target (", y_max_len, ")."])]):
            y_max_len = tf.identity(y_max_len)
        y_ta = tf.TensorArray(
          name="y_ta",
          dtype=y_data.dtype,
          element_shape=tf.TensorShape(y_data.copy_template_excluding_time_dim().batch_shape),
          size=y_max_len,
          infer_shape=True)
        y_ta = y_ta.unstack(y)

      # Note: tf.while_loop() will not give us all intermediate outputs, but we want them.
      # tf.scan() would do that but tf.scan() will loop over some input sequence -
      # however, that would not work because the input sequence is not fixed initially.
      # So, similar as tf.scan() does it, we collect all intermediate values.

      # In the while-loop, what we need to output is:
      # * next step counter (i)
      # * all outputs from layers which are in self.prev_layers_needed
      # * all hidden states from RnnCellLayer
      # * accumulated TensorArray of outputs from the output-layer for each step
      # For each of this, we need a sensible init, which we are supposed to return here.

      init_net_vars = cell.get_init_loop_vars()
      init_i = tf.constant(0)
      if have_known_seq_len:
        min_loop_len = max_seq_len
      else:
        min_loop_len = 0

      from collections import namedtuple
      OutputToAccumulate = namedtuple("OutputToAccumulate", ["name", "dtype", "element_shape", "get"])
      outputs_to_accumulate = []  # type: list[OutputToAccumulate]

      def add_output_to_acc(layer_name):
        name = "output_%s" % layer_name
        if any([(out.name == name) for out in outputs_to_accumulate]):
          return
        outputs_to_accumulate.append(OutputToAccumulate(
          name=name,
          dtype=cell.layer_data_templates[layer_name].output.dtype,
          element_shape=cell.layer_data_templates[layer_name].output.batch_shape,
          get=lambda: cell.net.layers[layer_name].output.placeholder))
      add_output_to_acc("output")

      layer_names_with_losses = []
      if self.network.eval_flag:  # only collect losses if we need them
        # Note about the subnet loss calculation:
        # 1. We can collect the output and calculate the loss on the whole sequence.
        # 2. We can calculate the loss on a frame base and collect it per frame.
        # We implemented option 1 (collect output, loss on sequence) earlier.
        # Option 1 had the following disadvantages:
        # - It can require a lot of extra memory if the output is large,
        #   e.g. with a softmax output of 30k classes.
        # - The loss calculation can be numerical unstable, e.g. for cross-entropy.
        #   This could be solved by also storing the output before the activation (e.g. softmax),
        #   which would require even more memory, and other cases is wasted (e.g. MSE loss).
        #   There is no good way to determine in advance if we need it or not.
        # Option 2 has the disadvantage that some part of the code might be more hacky.
        # Overall, option 2 is more straight-forward, probably more what the user intends,
        # can use numerical stable variants (e.g. for cross-entropy + softmax),
        # and is what we do now.

        # Not so nice but simple way to get all relevant layers:
        layer_names_with_losses = [
          layer.name for layer in cell.layer_data_templates.values()
          if layer.kwargs.get("loss", None)]

        def make_get_loss(layer_name, return_error=False, return_loss=False):
          """
          :param str layer_name:
          :param bool return_error:
          :param bool return_loss:
          :rtype: ()->tf.Tensor
          """

          def get_loss():
            layer = cell.net.layers[layer_name]
            assert layer.loss
            # This is a bit hacky but we want to not reduce the loss to a scalar
            # in the loop but get it as shape (batch,).
            # This should work with all current implementations
            # but might need some redesign later.
            layer.loss.reduce_func = lambda x: x
            if return_loss:
              value = layer.get_loss_value()
            elif return_error:
              value = layer.get_error_value()
            else:
              assert False, "return_error or return_loss"
            assert isinstance(value, tf.Tensor)
            value.set_shape(tf.TensorShape((None,)))  # (batch,)
            return value

          return get_loss

        for layer_name in layer_names_with_losses:
          outputs_to_accumulate.append(OutputToAccumulate(
            name="loss_%s" % layer_name,
            dtype=tf.float32,
            element_shape=(None,),  # (batch,)
            get=make_get_loss(layer_name, return_loss=True)))
          outputs_to_accumulate.append(OutputToAccumulate(
            name="error_%s" % layer_name,
            dtype=tf.float32,
            element_shape=(None,),  # (batch,)
            get=make_get_loss(layer_name, return_error=True)))

      output_beam_size = None
      collected_choices = []  # type: list[str]  # layer names
      if self.network.search_flag:
        for layer in cell.layer_data_templates.values():
          assert isinstance(layer, _TemplateLayer)
          if layer.search_choices:
            collected_choices += [layer.name]
            def get_derived(name):
              def get_choice_source_batches():
                layer = cell.net.layers[name]
                return layer.search_choices.src_beams
              return get_choice_source_batches
            outputs_to_accumulate += [
              OutputToAccumulate(
                name="choice_%s" % layer.name,
                dtype=tf.int32,
                element_shape=(None, layer.search_choices.beam_size),  # (batch, beam)
                get=get_derived(layer.name))]

        if collected_choices:
          output_beam_size = cell.layer_data_templates["output"].get_search_beam_size()
          assert output_beam_size is not None
          if seq_len is not None:
            from TFUtil import tile_transposed
            seq_len = tile_transposed(seq_len, axis=0, multiples=output_beam_size)  # (batch * beam,)

      if not have_known_seq_len:
        assert "end" in cell.layer_data_templates, (
          "You need to have an 'end' layer in your rec subnet if the generated seq len is unknown.")
        end_template = cell.layer_data_templates["end"]
        assert tf.as_dtype(end_template.output.dtype) is tf.bool
        assert end_template.output.batch_shape == (None,)  # (batch*beam,)
        assert end_template.output.sparse

      # Create a tensor array to store the intermediate values for each step i, e.g. of shape (batch, dim).
      init_acc_tas = [
        tf.TensorArray(
          name="acc_ta_%s" % out.name,
          dtype=out.dtype,
          element_shape=tf.TensorShape(out.element_shape),
          size=min_loop_len,
          dynamic_size=True,  # we will automatically grow it when needed
          infer_shape=True)
        for out in outputs_to_accumulate]

    def body(i, net_vars, acc_tas, seq_len_info=None):
      """
      The loop body of scan.

      :param tf.Tensor i: loop counter, scalar
      :param net_vars: the accumulator values
      :param list[tf.TensorArray] acc_tas: the output accumulator TensorArray
      :param (tf.Tensor,tf.Tensor)|None seq_len_info: tuple (end_flag, seq_len)
      :return: [i + 1, a_flat, tas]: the updated counter + new accumulator values + updated TensorArrays
      :rtype: (tf.Tensor, object, list[tf.TensorArray])

      Raises:
        TypeError: if initializer and fn() output structure do not match
        ValueType: if initializer and fn() output lengths do not match
      """
      # The inner scope name is a bit screwed up and this is nicer anyway.
      with reuse_name_scope(self._rec_scope.name + "/while_loop_body", absolute=True):
        net_vars = cell.get_next_loop_vars(
          net_vars,
          data=x_ta.read(i) if x_ta else None,
          classes=y_ta.read(i) if y_ta else None,
          i=i)
        if seq_len_info is not None:
          end_flag, dyn_seq_len = seq_len_info
          with tf.name_scope("end_flag"):
            # TODO: end_flag is (batch * beam_in,), probably a different beam than beam_out?
            end_flag = tf.logical_or(end_flag, cell.net.layers["end"].output.placeholder)  # (batch * beam,)
          with tf.name_scope("dyn_seq_len"):
            # TODO: also wrong...
            dyn_seq_len += tf.where(
              end_flag,
              constant_with_shape(0, shape=tf.shape(end_flag)),
              constant_with_shape(1, shape=tf.shape(end_flag)))  # (batch * beam,)
            seq_len_info = (end_flag, dyn_seq_len)
        else:
          end_flag = None
        # We could use tf.cond() to return the previous or so, and min_seq_len
        # to avoid the check if not needed. However, just filtering the result
        # outside the loop is likely faster.
        if collected_choices:
          # For the search choices, we do it here so that we can easily get out the final beam scores.
          with tf.name_scope("seq_filter_cond"):
            if seq_len is not None:
              seq_filter_cond = tf.less(i, seq_len)  # (batch * beam,)
            else:
              assert end_flag is not None
              seq_filter_cond = tf.logical_not(end_flag)
            seq_filter_cond = tf.reshape(seq_filter_cond, [batch_dim, output_beam_size])  # (batch, beam)
          for name in collected_choices:
            with reuse_name_scope(name):
              cell.net.layers[name].search_choices.filter_seqs(seq_filter_cond)
        assert len(acc_tas) == len(outputs_to_accumulate)
        acc_tas = [
          acc_ta.write(i, out.get(), name="%s_acc_ta_write" % out.name)
          for (acc_ta, out) in zip(acc_tas, outputs_to_accumulate)]
        res = (i + 1, net_vars, acc_tas)
        if seq_len_info is not None:
          res += (seq_len_info,)
        return res

    def cond(i, net_vars, acc_ta, seq_len_info=None):
      res = tf.less(i, max_seq_len)
      if seq_len_info is not None:
        end_flag, _ = seq_len_info
        res = tf.logical_and(res, tf.reduce_any(tf.logical_not(end_flag)))
      return res

    from TFUtil import constant_with_shape
    init_loop_vars = (init_i, init_net_vars, init_acc_tas)
    if not have_known_seq_len:
      # See body().
      out_batch_dim = cell.layer_data_templates["end"].get_batch_dim()
      init_seq_len_info = (
        constant_with_shape(False, shape=[out_batch_dim], name="initial_end_flag"),
        constant_with_shape(0, shape=[out_batch_dim], name="initial_seq_len"))
      init_loop_vars += (init_seq_len_info,)
    final_loop_vars = tf.while_loop(
      cond=cond,
      body=body,
      loop_vars=init_loop_vars)
    if have_known_seq_len:
      _, final_net_vars, final_acc_tas = final_loop_vars
    else:
      _, final_net_vars, final_acc_tas, (_, seq_len) = final_loop_vars
    if self.output.size_placeholder is None:
      self.output.size_placeholder = {}
    self.output.size_placeholder[0] = seq_len
    assert isinstance(final_acc_tas, list)
    assert isinstance(final_acc_tas[0], tf.TensorArray)
    assert len(final_acc_tas) == len(outputs_to_accumulate)
    final_acc_tas_dict = {
      out.name: final_acc_ta
      for (final_acc_ta, out) in zip(final_acc_tas, outputs_to_accumulate)}  # type: dict[str,tf.TensorArray]

    if layer_names_with_losses:
      with tf.name_scope("sub_net_loss"):
        self._sub_loss = 0.0  # accumulated
        for layer_name in sorted(layer_names_with_losses):
          layer_with_loss_inst = cell.net.layers[layer_name]
          loss_value = final_acc_tas_dict["loss_%s" % layer_name].stack(name="loss_%s_stack" % layer_name)
          error_value = final_acc_tas_dict["error_%s" % layer_name].stack(name="error_%s_stack" % layer_name)
          loss_value.set_shape(tf.TensorShape((None, None)))  # (time, batch)
          error_value.set_shape(tf.TensorShape((None, None)))  # (time, batch)
          loss_norm_factor = 1.0 / tf.cast(tf.reduce_sum(seq_len), tf.float32)

          from TFUtil import sequence_mask_time_major
          mask = sequence_mask_time_major(seq_len)
          loss_value = tf.where(mask, loss_value, tf.zeros_like(loss_value))
          error_value = tf.where(mask, error_value, tf.zeros_like(error_value))
          loss_value = tf.reduce_sum(loss_value)
          error_value = tf.reduce_sum(error_value)

          self._sub_loss += loss_value * layer_with_loss_inst.loss_scale
          # Only one error, not summed up. Determined by sorted layers.
          self._sub_error = error_value
          self._sub_loss_normalization_factor = loss_norm_factor

    # Check if collected_choices has all the right layers.
    # At the moment, _TemplateLayer.has_search_choices() might be incomplete, that is why we check here.
    for layer in cell.net.layers.values():
      if layer.name.startswith("prev:"):
        continue
      if layer.search_choices:
        assert layer.name in collected_choices
    for name in collected_choices:
      layer = cell.net.layers[name]
      assert layer.search_choices

    if collected_choices:
      # Find next choice layer. Then iterate through its source choice layers through time
      # and resolve the output over time to be in line with the final output search choices.
      output_choice_base = cell.net.get_search_choices(src=self.cell.net.layers["output"])
      assert isinstance(output_choice_base, LayerBase)
      assert output_beam_size == output_choice_base.search_choices.beam_size
      initial_beam_choices = tf.range(0, output_beam_size)  # (beam_out,)
      from TFUtil import expand_dims_unbroadcast
      initial_beam_choices = expand_dims_unbroadcast(
        initial_beam_choices, axis=0, dim=batch_dim)  # (batch, beam_out)

      new_acc_output_ta = tf.TensorArray(
        name="new_acc_output_ta",
        dtype=cell.layer_data_templates["output"].output.dtype,
        element_shape=tf.TensorShape(cell.layer_data_templates["output"].output.batch_shape),
        size=final_acc_tas[0].size(),
        infer_shape=True)

      def search_resolve_body(i, choice_beams, new_acc_output_ta):
        # This loops goes backwards through time.
        # This starts at i == seq_len - 1.
        # choice_beams are from the previous step, shape (batch, beam_out) -> beam idx of output,
        # output is of shape (batch * beam, n_out).
        with reuse_name_scope(self._rec_scope.name + "/while_loop_search_body", absolute=True):
          # We start at the output layer choice base, and search for its source, i.e. for the previous time frame.
          choice_base = output_choice_base
          is_output_choice = True
          while True:
            assert choice_base.network is self.cell.net, "not yet implemented otherwise"

            src_choice_beams = final_acc_tas_dict["choice_%s" % choice_base.name].read(i)  # (batch, beam) -> beam_in idx
            assert src_choice_beams.get_shape().ndims == 2

            with tf.name_scope("choice_beams"):
              from TFUtil import nd_indices, assert_min_tf_version
              assert_min_tf_version((1, 1), "gather_nd")
              idxs_exp = nd_indices(choice_beams)  # (batch, beam_out, 2) -> (batch idx, beam idx)
              src_choice_beams = tf.gather_nd(src_choice_beams, idxs_exp)  # (batch, beam_out)
            if is_output_choice:
              with tf.name_scope("output"):
                output = final_acc_tas_dict["output_output"].read(i)  # (batch * beam, [n_out])
                out_shape = list(self.output.batch_shape[1:])  # without time-dim
                output.set_shape(tf.TensorShape(out_shape))
                output = tf.reshape(
                  output,
                  [batch_dim,
                   output_beam_size] + out_shape[1:])  # (batch, beam, [n_out])
                output = tf.gather_nd(output, idxs_exp)  # (batch, beam_par, [n_out])
                output = tf.reshape(
                  output,
                  [batch_dim * output_beam_size] + out_shape[1:])  # (batch * beam_par, [n_out])
                new_acc_output_ta = new_acc_output_ta.write(i, output)

            assert choice_base.search_choices
            src_choice_layer = choice_base.search_choices.src_layer
            assert src_choice_layer is not None  # must be one, e.g. from prev time frame
            if isinstance(src_choice_layer, _TemplateLayer):
              assert src_choice_layer.is_prev_time_frame
              return (
                i - 1,
                src_choice_beams,
                new_acc_output_ta)
            is_output_choice = False
            choice_base = src_choice_layer
            choice_beams = src_choice_beams

      _, _, new_acc_output_ta = tf.while_loop(
        cond=(lambda i, *args: tf.greater_equal(i, 0)),
        body=search_resolve_body,
        loop_vars=(
          final_acc_tas[0].size() - 1,  # initial i. we go backwards
          initial_beam_choices,
          new_acc_output_ta))
      final_acc_tas_dict["output_output"] = new_acc_output_ta

      # Collect the search choices for the rec layer itself.
      # Our output will be of shape (time, batch * beam, dim).
      # The beam scores will be of shape (batch, beam).
      self.search_choices = SearchChoices(owner=self, beam_size=output_beam_size)
      # TODO search_choices.src_beams, not really supported currently
      final_choice_rec_vars = cell.get_layer_rec_var_from_loop_vars(
        loop_vars=final_net_vars,
        layer_name=output_choice_base.name)
      self.search_choices.set_beam_scores_from_rec(final_choice_rec_vars)
      assert output_beam_size == self.get_search_beam_size()

    output = final_acc_tas_dict["output_output"].stack()  # e.g. (time, batch, dim)
    if not have_known_seq_len:
      with tf.name_scope("output_sub_slice"):
        output = output[:tf.reduce_max(seq_len)]  # usually one less
    return output

  def get_last_hidden_state(self):
    assert self._last_hidden_state is not None, (
      "last-hidden-state not implemented/supported for this layer-type. try another unit. see the code.")
    return self._last_hidden_state


class _SubnetworkRecCell(object):
  def __init__(self, net_dict, parent_rec_layer=None, parent_net=None, source_data=None):
    """
    :param dict[str] net_dict:
    :param RecLayer parent_rec_layer:
    :param TFNetwork.TFNetwork parent_net:
    :param Data|None source_data: usually concatenated input from the rec-layer
    """
    from copy import deepcopy
    if parent_net is None and parent_rec_layer:
      parent_net = parent_rec_layer.network
    if source_data is None and parent_rec_layer:
      source_data = parent_rec_layer.input_data
    self.parent_rec_layer = parent_rec_layer
    self.parent_net = parent_net
    self.net_dict = deepcopy(net_dict)
    from TFNetwork import TFNetwork, ExternData
    self.net = TFNetwork(
      name="%s/%s:rec-subnet" % (parent_net.name, parent_rec_layer.name if parent_rec_layer else "?"),
      extern_data=ExternData(),
      train_flag=parent_net.train_flag,
      search_flag=parent_net.search_flag,
      parent_layer=parent_rec_layer,
      parent_net=parent_net)
    if source_data:
      self.net.extern_data.data["source"] = \
          source_data.copy_template_excluding_time_dim()
    for key in parent_net.extern_data.data.keys():
      self.net.extern_data.data[key] = \
        parent_net.extern_data.data[key].copy_template_excluding_time_dim()
    self.layer_data_templates = {}  # type: dict[str,_TemplateLayer]
    self.prev_layers_needed = set()  # type: set[str]
    self._construct_template()
    self._initial_outputs = None  # type: dict[str,tf.Tensor]
    self._initial_extra_outputs = None  # type: dict[str,dict[str,tf.Tensor|tuple[tf.Tensor]]]

  def _construct_template(self):
    """
    Without creating any computation graph, create TemplateLayer instances.
    """
    def add_templated_layer(name, layer_class, **layer_desc):
      """
      This is used instead of self.net.add_layer because we don't want to add
      the layers at this point, we just want to construct the template layers
      and store inside self.layer_data_templates.

      :param str name:
      :param type[LayerBase]|LayerBase layer_class:
      :param dict[str] layer_desc:
      :rtype: LayerBase
      """
      # _TemplateLayer already created in get_templated_layer.
      layer = self.layer_data_templates[name]
      layer_desc = layer_desc.copy()
      layer_desc["name"] = name
      layer_desc["network"] = self.net
      output = layer_class.get_out_data_from_opts(**layer_desc)
      layer.init(layer_class=layer_class, output=output, **layer_desc)
      return layer

    class construct_ctx:
      # Stack of layers:
      layers = []  # type: list[_TemplateLayer]

    def get_templated_layer(name):
      """
      :param str name:
      :rtype: _TemplateLayer|LayerBase
      """
      if name.startswith("prev:"):
        name = name[len("prev:"):]
        self.prev_layers_needed.add(name)
      if name in self.layer_data_templates:
        layer = self.layer_data_templates[name]
        construct_ctx.layers[-1].dependencies.add(layer)
        return layer
      if name.startswith("base:"):
        layer = self.parent_net.layers[name[len("base:"):]]
        construct_ctx.layers[-1].dependencies.add(layer)
        return layer
      # Need to create layer instance here now to not run into recursive loops.
      # We will extend it later in add_templated_layer().
      layer = _TemplateLayer(name=name, network=self.net)
      if construct_ctx.layers:
        construct_ctx.layers[-1].dependencies.add(layer)
      construct_ctx.layers.append(layer)
      self.layer_data_templates[name] = layer
      self.net._construct_layer(
        self.net_dict, name, get_layer=get_templated_layer, add_layer=add_templated_layer)
      assert construct_ctx.layers[-1] is layer
      construct_ctx.layers.pop(-1)
      return layer

    assert not self.layer_data_templates, "do not call this multiple times"
    get_templated_layer("output")
    assert "output" in self.layer_data_templates
    assert not construct_ctx.layers

    if "end" in self.net_dict:  # used to specify ending of a sequence
      get_templated_layer("end")

  def _construct(self, prev_outputs, prev_extra, i, data=None, classes=None):
    """
    :param dict[str,tf.Tensor] prev_outputs: outputs of the layers from the previous step
    :param dict[str,dict[str,tf.Tensor]] prev_extra: extra output / hidden states of the previous step for layers
    :param tf.Tensor i: loop counter
    :param tf.Tensor|None data: optional source data, shape e.g. (batch,dim)
    :param tf.Tensor|None classes: optional target classes, shape e.g. (batch,) if it is sparse
    """
    assert not self.net.layers, "do not call this multiple times"
    if data is not None:
      self.net.extern_data.data["source"].placeholder = data
    if classes is not None:
      self.net.extern_data.data[self.parent_rec_layer.target].placeholder = classes
    for data_key, data in self.net.extern_data.data.items():
      if data_key not in self.net.used_data_keys:
        continue
      if data.placeholder is None:
        raise Exception("rec layer %r subnet data key %r is not set" % (self.parent_rec_layer.name, data_key))

    prev_layers = {}  # type: dict[str,_TemplateLayer]
    for name in set(list(prev_outputs.keys()) + list(prev_extra.keys())):
      self.net.layers["prev:%s" % name] = prev_layers[name] = self.layer_data_templates[name].copy_as_prev_time_frame(
        prev_output=prev_outputs.get(name, None),
        rec_vars_prev_outputs=prev_extra.get(name, None))
    extended_layers = {}

    from copy import deepcopy
    net_dict = deepcopy(self.net_dict)
    for name in net_dict.keys():
      if name in prev_layers:
        net_dict[name]["rec_previous_layer"] = prev_layers[name]

    def get_layer(name):
      if name.startswith("prev:"):
        return prev_layers[name[len("prev:"):]]
      if name.startswith("base:"):
        if name in extended_layers:
          return extended_layers[name]
        l = self.parent_net.layers[name[len("base:"):]]
        if self.parent_net.search_flag:
          needed_beam_size = self.layer_data_templates["output"].output.beam_size
          if needed_beam_size:
            if l.output.beam_size != needed_beam_size:
              from TFNetworkLayer import InternalLayer
              l = InternalLayer(name=name, network=self.net, output=l.output.copy_extend_with_beam(needed_beam_size))
              extended_layers[name] = l
          assert l.output.beam_size == needed_beam_size
        return l
      return self.net._construct_layer(net_dict, name=name, get_layer=get_layer)

    self.net.layers[":i"] = _StepIndexLayer(i=i, name=":i", network=self.net)
    get_layer("output")
    assert "output" in self.net.layers
    # Might not be resolved otherwise:
    for name in self.prev_layers_needed:
      get_layer(name)
    if "end" in self.net_dict:  # used to specify ending of a sequence
      get_layer("end")

  def _get_init_output(self, name):
    """
    :param str name: layer name
    :rtype: tf.Tensor
    """
    template_layer = self.layer_data_templates[name]
    cl = template_layer.layer_class_type
    batch_dim = template_layer.get_batch_dim()
    if name == "end" and template_layer.kwargs.get("initial_output", None) is None:
      # Special case for the 'end' layer.
      from TFUtil import constant_with_shape
      return constant_with_shape(False, shape=[batch_dim], name="initial_end")
    return cl.get_rec_initial_output(batch_dim=batch_dim, **self.layer_data_templates[name].kwargs)

  def _get_init_extra_outputs(self, name):
    """
    :param str name: layer name
    :rtype: tf.Tensor|tuple[tf.Tensor]
    """
    template_layer = self.layer_data_templates[name]
    cl = template_layer.layer_class_type
    batch_dim = template_layer.get_batch_dim()
    d = cl.get_rec_initial_extra_outputs(batch_dim=batch_dim, **self.layer_data_templates[name].kwargs)
    return d

  def check_output_template_shape(self):
    output_template = self.layer_data_templates["output"]
    assert output_template.output.dim == self.parent_rec_layer.output.dim
    assert self.parent_rec_layer.output.time_dim_axis == 0
    assert output_template.output.time_dim_axis is None
    assert output_template.output.batch_shape == self.parent_rec_layer.output.batch_shape[1:], (
      "see RecLayer.get_out_data_from_opts()")

  def get_next_loop_vars(self, loop_vars, i, data=None, classes=None):
    """
    :param (list[tf.Tensor],list[tf.Tensor]) loop_vars: loop_vars from the previous step
    :param tf.Tensor i: loop counter
    :param tf.Tensor|None data: optional source data, shape e.g. (batch,dim)
    :param tf.Tensor|None classes: optional target classes, shape e.g. (batch,) if it is sparse
    :return: next loop_vars
    :rtype: (list[tf.Tensor],list[tf.Tensor|tuple[tf.Tensor]])
    """
    from TFUtil import identity_op_nested
    from Util import sorted_values_from_dict, dict_zip
    prev_outputs_flat, prev_extra_flat = loop_vars
    assert len(prev_outputs_flat) == len(self.prev_layers_needed)
    prev_outputs = {k: v for (k, v) in zip(sorted(self.prev_layers_needed), prev_outputs_flat)}
    with tf.name_scope("prev_outputs"):
      prev_outputs = {k: tf.identity(v, name=k) for (k, v) in prev_outputs.items()}
    assert len(prev_extra_flat) == len(self._initial_extra_outputs)
    prev_extra = {
      k: dict_zip(sorted(self._initial_extra_outputs[k]), v)
      for (k, v) in zip(sorted(self._initial_extra_outputs), prev_extra_flat)}
    with tf.name_scope("prev_extra"):
      prev_extra = identity_op_nested(prev_extra)
    with reuse_name_scope(self.parent_rec_layer._rec_scope):
      self._construct(prev_outputs=prev_outputs, prev_extra=prev_extra, data=data, classes=classes, i=i)
    outputs_flat = [self.net.layers[k].output.placeholder for k in sorted(self.prev_layers_needed)]
    extra_flat = [
      sorted_values_from_dict(self.net.layers[k].rec_vars_outputs)
      for k in sorted(self.layer_data_templates)
      if self.net.layers[k].rec_vars_outputs]
    return outputs_flat, extra_flat

  def get_init_loop_vars(self):
    """
    :return: initial loop_vars. see self.get_next_loop_vars()
    :rtype: (list[tf.Tensor],list[tf.Tensor|tuple[tf.Tensor]])
    """
    self._initial_outputs = {k: self._get_init_output(k) for k in self.prev_layers_needed}
    self._initial_extra_outputs = {
      k: self._get_init_extra_outputs(k) for k in self.layer_data_templates.keys()}
    self._initial_extra_outputs = {k: v for (k, v) in self._initial_extra_outputs.items() if v}
    from Util import sorted_values_from_dict
    init_outputs_flat = sorted_values_from_dict(self._initial_outputs)
    init_extra_flat = [sorted_values_from_dict(v) for (k, v) in sorted(self._initial_extra_outputs.items())]
    return init_outputs_flat, init_extra_flat

  def get_layer_rec_var_from_loop_vars(self, loop_vars, layer_name):
    """
    :param (list[tf.Tensor],list[tf.Tensor]) loop_vars: loop_vars like in self.get_next_loop_vars()
    :param str layer_name:
    :return: layer rec_vars_outputs
    :rtype: dict[str,tf.Tensor]
    """
    prev_outputs_flat, prev_extra_flat = loop_vars
    assert len(prev_outputs_flat) == len(self.prev_layers_needed)
    assert len(prev_extra_flat) == len(self._initial_extra_outputs)
    from Util import dict_zip
    prev_extra = {
      k: dict_zip(sorted(self._initial_extra_outputs[k]), v)
      for (k, v) in zip(sorted(self._initial_extra_outputs), prev_extra_flat)}
    return prev_extra[layer_name]

  def get_parent_deps(self):
    """
    :return: list of dependencies to the parent network
    :rtype: list[LayerBase]
    """
    l = []
    layers = self.net.layers
    if not layers:  # happens only during initialization
      layers = self.layer_data_templates
    for _, layer in sorted(layers.items()):
      assert isinstance(layer, LayerBase)
      for dep in layer.get_dep_layers():
        # Usually dep.network is self.cell.net but it could reference to our own net,
        # e.g. if this is an attention layer like
        # {"class": "dot_attention", "base": "base:encoder", ...}.
        if dep.network is self.parent_net:
          if dep not in l:
            l += [dep]
    return l

  def move_outside_loop(self):
    """
    Based on the templated network, we can see the dependencies.
    We want to move as much calculation, i.e. subnet layers, as possible out of the loop.

    :return:
    """
    layers_in_loop = [l for (_, l) in sorted(self.layer_data_templates.items())]
    input_layers_moved_out = []
    output_layers_moved_out = []
    needed_outputs = ["output"]  # TODO + losses + end + other?
    layers_needed_from_prev_frame = sorted(self.prev_layers_needed)

    def output_can_move_out(layer):
      assert isinstance(layer, _TemplateLayer)
      # layer.output from prev time frame is used by other layers?
      if layer.name in layers_needed_from_prev_frame:
        return False
      # layer.output is used by other layers?
      for other_layer in layers_in_loop:
        if layer in other_layer.get_dep_layers():
          return False
      return True

    def find_output_layer_to_move_out():
      for layer in layers_in_loop:
        if layer.name not in needed_outputs:
          continue
        if output_can_move_out(layer):
          return layer
      return None

    def output_move_out(layer):
      assert isinstance(layer, _TemplateLayer)
      needed_outputs.remove(layer.name)
      layers_in_loop.remove(layer)
      output_layers_moved_out.append(layer)

    def input_can_move_out(layer):
      assert isinstance(layer, _TemplateLayer)
      layer_deps = layer.get_dep_layers()
      # We depend on other layers from this sub-network?
      for other_layer in layers_in_loop:
        if other_layer in layer_deps:
          return False
      return True

    def find_input_layer_to_move_out():
      for layer in layers_in_loop:
        if input_can_move_out(layer):
          return layer
      return None

    def input_move_out(layer):
      assert isinstance(layer, _TemplateLayer)
      if layer.name in needed_outputs:
        needed_outputs.remove(layer.name)
      layers_in_loop.remove(layer)
      input_layers_moved_out.append(layer)

    while True:
      output_layer = find_output_layer_to_move_out()
      if output_layer:
        output_move_out(output_layer)
      input_layer = find_input_layer_to_move_out()
      if input_layer:
        input_move_out(input_layer)
      if not output_layer and not input_layer:
        break

    log_stream = log.v3
    print("Rec layer sub net:", file=log_stream)
    print("  Input layers moved out of loop: (#: %i)" % len(input_layers_moved_out), file=log_stream)
    for layer in input_layers_moved_out:
      print("    %s" % layer.name, file=log_stream)
    if not input_layers_moved_out:
      print("    None", file=log_stream)
    print("  Output layers moved out of loop: (#: %i)" % len(output_layers_moved_out), file=log_stream)
    for layer in output_layers_moved_out:
      print("    %s" % layer.name, file=log_stream)
    if not output_layers_moved_out:
      print("    None", file=log_stream)


class _TemplateLayer(LayerBase):
  """
  Used by _SubnetworkRecCell.
  In a first pass, it creates template layers with only the meta information about the Data.
  All "prev:" layers also stay instances of _TemplateLayer in the real computation graph.
  """

  def __init__(self, network, name):
    """
    :param TFNetwork.TFNetwork network:
    :param str name:
    """
    # Init with some dummy.
    super(_TemplateLayer, self).__init__(
      out_type={"shape": ()}, name=name, network=network)
    self.output.size_placeholder = {}  # must be initialized
    self.layer_class = ":uninitialized-template"
    self.is_data_template = False
    self.is_prev_time_frame = False
    self.layer_class_type = None  # type: type[LayerBase]|LayerBase
    self.kwargs = None  # type: dict[str]
    self.dependencies = set()  # type: set[LayerBase]
    self._template_base = None  # type: _TemplateLayer

  def __repr__(self):
    return "<%s(%s)(%s) %r out_type=%s>" % (
      self.__class__.__name__, self.layer_class_type.__name__ if self.layer_class_type else None, self.layer_class,
      self.name, self.output.get_description(with_name=False))

  def init(self, output, layer_class, template_type="template", **kwargs):
    """
    :param Data output:
    :param type[LayerBase]|LayerBase layer_class:
    :param str template_type:
    """
    # Overwrite self.__class__ so that checks like isinstance(layer, ChoiceLayer) work.
    # Not sure if this is the nicest way -- probably not, so I guess this will go away later.
    self.is_prev_time_frame = (template_type == "prev")
    self.is_data_template = (template_type == "template")
    assert self.is_prev_time_frame or self.is_data_template
    self.layer_class = ":%s:%s" % (template_type, layer_class.layer_class)
    self.output = output
    if not self.output.size_placeholder:
      self.output.size_placeholder = {}
    self.layer_class_type = layer_class
    self.kwargs = kwargs
    self.kwargs["output"] = output
    if self._has_search_choices():
      self.search_choices = SearchChoices(owner=self, beam_size=self._get_search_choices_beam_size())

  def copy_as_prev_time_frame(self, prev_output=None, rec_vars_prev_outputs=None):
    """
    :param tf.Tensor|None prev_output:
    :param dict[str,tf.Tensor]|None rec_vars_prev_outputs:
    :return: new _TemplateLayer
    :rtype: _TemplateLayer
    """
    l = _TemplateLayer(network=self.network, name="prev:%s" % self.name)
    l._template_base = self
    l.dependencies = self.dependencies
    l.init(layer_class=self.layer_class_type, template_type="prev", **self.kwargs)
    if prev_output is not None:
      l.output.placeholder = prev_output
      l.output.placeholder.set_shape(tf.TensorShape(l.output.batch_shape))
      assert l.output.placeholder.dtype is tf.as_dtype(l.output.dtype)
      l.output.size_placeholder = {}  # must be set
    if rec_vars_prev_outputs is not None:
      l.rec_vars_outputs = rec_vars_prev_outputs
    if self.search_choices:
      l.search_choices = SearchChoices(owner=l, beam_size=self.search_choices.beam_size)
      l.search_choices.set_beam_scores_from_own_rec()
      l.output.beam_size = self.search_choices.beam_size
    return l

  def get_dep_layers(self):
    if self.is_data_template:
      # This is from the template construction, a layer in _SubnetworkRecCell.layer_data_templates.
      # Maybe we already have the layer constructed.
      real_layer = self.network.layers.get(self.name)
      if real_layer:
        return real_layer.get_dep_layers()
      # All refs to this subnet are other _TemplateLayer, no matter if prev-frame or not.
      # Otherwise, refs to the base network are given as-is.
      return sorted(self.dependencies, key=lambda l: l.name)
    assert self.is_prev_time_frame
    # In the current frame, the deps would be self.dependencies.
    # (It's ok that this would not contain prev-frames.)
    # We want to return the logical dependencies here, i.e. all such layers from previous frames.
    # Not all of them might exist, but then, we want to get their dependencies.
    cur_deps = sorted(self.dependencies, key=lambda l: l.name)
    deps = []
    for layer in cur_deps:
      if layer.network is not self.network:
        if layer not in deps:
          deps.append(layer)
        continue
      assert isinstance(layer, _TemplateLayer)
      assert layer.is_data_template
      # Find the related prev-frame layer.
      prev_layer = self.network.layers.get("prev:%s" % layer.name, None)
      if prev_layer:
        if prev_layer not in deps:
          deps.append(prev_layer)
        continue
      # Not yet constructed or not needed to construct.
      # In that case, add its dependencies instead.
      layer_deps = sorted(layer.dependencies, key=lambda l: l.name)
      for dep in layer_deps:
        if dep not in cur_deps:
          cur_deps.append(dep)  # the current iterable will also visit this
    return deps

  def _has_search_choices(self):
    """
    :return: whether an instance of this class has search_choices set
    :rtype: bool
    """
    # TODO: extend if this is a subnet or whatever
    if not self.network.search_flag:
      return False
    return issubclass(self.layer_class_type, ChoiceLayer)

  def _get_search_choices_beam_size(self):
    """
    Only valid if self.has_search_choices() is True.
    :rtype: int
    """
    return self.kwargs["beam_size"]


class _StepIndexLayer(LayerBase):
  """
  Used by _SubnetworkRecCell.
  Represents the current step number.
  """

  layer_class = ":i"

  def __init__(self, i, **kwargs):
    super(_StepIndexLayer, self).__init__(
      output=Data(name="i", shape=(), dtype="int32", sparse=False, placeholder=tf.expand_dims(i, axis=0)),
      **kwargs)


class RnnCellLayer(_ConcatInputLayer):
  """
  Wrapper around tf.contrib.rnn.RNNCell.
  This will operate a single step, i.e. there is no time dimension,
  i.e. we expect a (batch,n_in) input, and our output is (batch,n_out).
  This is expected to be used inside a RecLayer.
  """

  layer_class = "rnn_cell"

  def __init__(self, n_out, unit, initial_state=None, unit_opts=None, **kwargs):
    """
    :param int n_out: so far, only output shape (batch,n_out) supported
    :param str|tf.contrib.rnn.RNNCell unit: e.g. "BasicLSTM" or "LSTMBlock"
    :param str|float|LayerBase|tuple[LayerBase]|dict[LayerBase] initial_state: see self._get_rec_initial_state().
      This will be set via transform_config_dict().
      To get the state from another recurrent layer, use the GetLastHiddenStateLayer (get_last_hidden_state).
    :param dict[str]|None unit_opts: passed to the cell.__init__
    """
    super(RnnCellLayer, self).__init__(**kwargs)
    self._initial_state = initial_state
    with tf.variable_scope(
          "rec",
          initializer=tf.contrib.layers.xavier_initializer(
            seed=self.network.random.randint(2**31))) as scope:
      assert isinstance(scope, tf.VariableScope)
      scope_name_prefix = scope.name + "/"  # e.g. "layer1/rec/"
      self.cell = self._get_cell(n_out=n_out, unit=unit, unit_opts=unit_opts)
      self.output.time_dim_axis = None
      self.output.batch_dim_axis = 0
      prev_state = self._rec_previous_layer.rec_vars_outputs["state"]
      self.output.placeholder, state = self.cell(self.input_data.placeholder, prev_state)
      self._hidden_state = state
      self.rec_vars_outputs["state"] = state
      params = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope=scope_name_prefix)
      assert params
      self.params.update({p.name[len(scope_name_prefix):-2]: p for p in params})

  @classmethod
  def _get_cell(cls, n_out, unit, unit_opts=None):
    """
    :param int n_out:
    :param str|tf.contrib.rnn.RNNCell unit:
    :param dict[str]|None unit_opts:
    :rtype: tf.contrib.rnn.RNNCell
    """
    import tensorflow.contrib.rnn as rnn_contrib
    if isinstance(unit, rnn_contrib.RNNCell):
      return unit
    rnn_cell_class = RecLayer.get_rnn_cell_class(unit)
    assert issubclass(rnn_cell_class, rnn_contrib.RNNCell)
    if unit_opts is None:
      unit_opts = {}
    assert isinstance(unit_opts, dict)
    # This should not have any side-effects, i.e. it should not add to the current computation graph,
    # it should also not create any vars yet, etc.
    cell = rnn_cell_class(n_out, **unit_opts)
    assert isinstance(cell, rnn_contrib.RNNCell)
    return cell

  @classmethod
  def get_out_data_from_opts(cls, n_out, name, sources=(), **kwargs):
    beam_size = None
    for dep in sources:
      beam_size = beam_size or dep.output.beam_size
    return Data(
      name="%s_output" % name,
      shape=(n_out,), dim=n_out,
      batch_dim_axis=0,
      time_dim_axis=None,
      size_placeholder={},
      beam_size=beam_size)

  def get_dep_layers(self):
    l = list(super(RnnCellLayer, self).get_dep_layers())

    def visit(s):
      if isinstance(s, (list, tuple)):
        for x in s:
          visit(x)
      elif isinstance(s, dict):
        for x in s.values():
          visit(x)
      elif isinstance(s, LayerBase):
        l.append(s)
      else:
        assert isinstance(s, (str, int, float, type(None)))

    visit(self._initial_state)
    return l

  @classmethod
  def get_hidden_state_size(cls, n_out, unit, unit_opts=None, **kwargs):
    """
    :return: size or tuple of sizes
    :rtype: int|tuple[int]
    """
    cell = cls._get_cell(unit=unit, unit_opts=unit_opts, n_out=n_out)
    import tensorflow.contrib.rnn as rnn_contrib
    assert isinstance(cell, rnn_contrib.RNNCell)
    return cell.state_size

  def get_hidden_state(self):
    return self._hidden_state

  def get_last_hidden_state(self):
    from tensorflow.python.util import nest
    if nest.is_sequence(self._hidden_state):
      return tf.concat(self._hidden_state, axis=1)
    return self._hidden_state

  @classmethod
  def _get_rec_initial_state(cls, batch_dim, name, initial_state=None, **kwargs):
    init_value = initial_state
    dim = cls.get_hidden_state_size(**kwargs)

    def make(d, v):
      assert isinstance(d, int)
      assert isinstance(v, (LayerBase, int, float, str, type(None)))
      shape = [batch_dim, d]
      if isinstance(v, LayerBase):
        h = v.get_last_hidden_state()
        if h is not None:
          h.set_shape(tf.TensorShape((None, d)))
          return h
        assert v.output.batch_dim_axis == 0
        assert v.output.time_dim_axis is None
        assert v.output.shape == (d,)
        return v.output.placeholder
      elif v == "zeros" or not v:
        return tf.zeros(shape)
      elif v == "ones" or v == 1:
        return tf.ones(shape)
      else:
        raise Exception("invalid initial state type %r for sub-layer %r" % (v, name))

    def make_list():
      if isinstance(init_value, (list, tuple)):
        assert len(init_value) == len(dim)
        return [make(d, v_) for (d, v_) in zip(dim, init_value)]
      # Do not broadcast LayerBase automatically in this case.
      assert isinstance(init_value, (int, float, str, type(None)))
      return [make(d, init_value) for d in dim]

    # Make it the same type because nest.assert_same_structure() will complain otherwise.
    if isinstance(dim, tuple) and type(dim) is not tuple:  # assume namedtuple
      keys = dim._fields
      assert len(dim) == len(keys)
      assert isinstance(init_value, (int, float, str, tuple, list, dict, type(None)))
      if not isinstance(init_value, dict) and init_value not in (0, 1, None):
        print("RnnCellLayer %r: It is recommended to use a dict to specify 'initial_state' with keys %r for the state dimensions %r." % (name, keys, dim), file=log.v2)
      if isinstance(init_value, dict):
        assert set(init_value.keys()) == set(keys), "You must specify all keys for the state dimensions %r." % dim
        assert len(init_value) == len(dim)
        s = {k: make(d, init_value[k]) for (k, d) in zip(keys, dim)}
      else:
        s = make_list()
        assert len(s) == len(keys)
        s = {k: s_ for (k, s_) in zip(keys, s)}
      return type(dim)(**s)
    elif isinstance(dim, (tuple, list)):
      s = make_list()
      assert len(s) == len(dim)
      return type(dim)(s)
    elif isinstance(dim, int):
      return make(dim, init_value)
    else:
      raise Exception("Did not expect hidden_state_size %r." % dim)

  @classmethod
  def get_rec_initial_extra_outputs(cls, **kwargs):
    return {"state": cls._get_rec_initial_state(**kwargs)}

  @classmethod
  def transform_config_dict(cls, d, network, get_layer):
    """
    :param dict[str] d: will modify inplace
    :param TFNetwork.TFNetwork network:
    :param ((str) -> LayerBase) get_layer: function to get or construct another layer
    """
    super(RnnCellLayer, cls).transform_config_dict(d, network=network, get_layer=get_layer)
    if "initial_state" in d:
      def resolve(v):
        if isinstance(v, str):
          if v in ["zeros", "ones"]:
            return v
          return get_layer(v)
        if isinstance(v, (tuple, list)):
          return [resolve(x) for x in v]
        if isinstance(v, dict):
          return {k: resolve(x) for (k, x) in v.items()}
        if isinstance(v, (float, int)):
          return v
        if v is None:
          return v
        raise Exception("%r: invalid type: %r, %r" % (d, v, type(v)))
      d["initial_state"] = resolve(d["initial_state"])


class GetLastHiddenStateLayer(LayerBase):
  """
  Will combine (concat or add or so) all the last hidden states from all sources.
  """

  layer_class = "get_last_hidden_state"

  def __init__(self, n_out, combine="concat", **kwargs):
    """
    :param int n_out: dimension. output will be of shape (batch, n_out)
    :param str combine: "concat" or "add"
    """
    super(GetLastHiddenStateLayer, self).__init__(**kwargs)
    assert len(self.sources) > 0
    sources = [s.get_last_hidden_state() for s in self.sources]
    assert all([s is not None for s in sources])
    if len(sources) == 1:
      h = sources[0]
    else:
      if combine == "concat":
        h = tf.concat(sources, axis=1, name="concat_hidden_states")
      elif combine == "add":
        h = tf.add_n(sources, name="add_hidden_states")
      else:
        raise Exception("invalid hidden states combine mode %r" % combine)
    from TFUtil import check_input_ndim, check_input_dim
    h = check_input_ndim(h, 2)
    h = check_input_dim(h, 1, n_out)
    self.output.placeholder = h

  def get_last_hidden_state(self):
    return self.output.placeholder

  @classmethod
  def get_out_data_from_opts(cls, n_out, **kwargs):
    return super(GetLastHiddenStateLayer, cls).get_out_data_from_opts(
      out_type={"shape": (n_out,), "dim": n_out, "batch_dim_axis": 0, "time_dim_axis": None}, **kwargs)


class ChoiceLayer(LayerBase):
  """
  This layer represents a choice to be made in search during inference,
  such as choosing the top-k outputs from a log-softmax for beam search.
  During training, this layer can return the true label.
  This is supposed to be used inside the rec layer.
  This can be extended in various ways.

  We present the scores in +log space, and we will add them up along the path.
  Assume that we get input (batch,dim) from a (log-)softmax.
  Assume that each batch is already a choice via search.
  In search with a beam size of N, we would output
  sparse (batch=N,) and scores for each.
  """
  layer_class = "choice"

  def __init__(self, beam_size, input_type="prob", **kwargs):
    """
    :param int beam_size: the outgoing beam size. i.e. our output will be (batch * beam_size, ...)
    :param str input_type: "prob" or "log_prob", whether the input is in probability space, log-space, etc.
      or "regression", if it is a prediction of the data as-is.
    """
    super(ChoiceLayer, self).__init__(**kwargs)
    # We assume log-softmax here, inside the rec layer.
    assert len(self.sources) == 1
    assert not self.sources[0].output.sparse
    assert self.sources[0].output.dim == self.output.dim
    assert self.sources[0].output.shape == (self.output.dim,)
    assert self.target
    if self.network.search_flag:
      # We are doing the search.
      # We don't do any checking for sequence length here.
      # The logic is implemented in `SearchChoices.filter_seqs()`.
      # TODO this is wrong, we must do it here
      self.search_choices = SearchChoices(
        owner=self,
        beam_size=beam_size)
      if input_type == "regression":
        # It's not a probability distribution, so there is no search here.
        assert self.search_choices.beam_size == 1
        self.output = self.sources[0].output.copy_compatible_to(self.output)
      else:
        net_batch_dim = self.network.get_batch_dim()
        assert self.search_choices.src_layer, (
          self.network.debug_search_choices(base_search_choice=self),
          "Not implemented yet. In rec-layer, we would always have our prev-frame as one previous search choice. "
          "Our deps: %r" % self.get_dep_layers())
        scores_base = self.search_choices.src_layer.search_choices.beam_scores  # (batch, beam_in)
        assert scores_base.get_shape().ndims == 2, "%r invalid" % self.search_choices.src_layer.search_choices
        beam_in = tf.shape(scores_base)[1]
        scores_base = tf.expand_dims(scores_base, axis=-1)  # (batch, beam_in, dim)
        scores_in = self.sources[0].output.placeholder  # (batch * beam_in, dim)
        # We present the scores in +log space, and we will add them up along the path.
        if input_type == "prob":
          scores_in = tf.log(scores_in)
        elif input_type == "log_prob":
          pass
        else:
          raise Exception("%r: invalid input type %r" % (self, input_type))
        scores_in_dim = self.sources[0].output.dim
        scores_in = tf.reshape(scores_in, [net_batch_dim, beam_in, scores_in_dim])  # (batch, beam_in, dim)
        scores_in += scores_base  # (batch, beam_in, dim)
        scores_in_flat = tf.reshape(scores_in, [net_batch_dim, beam_in * scores_in_dim])  # (batch, beam_in * dim)
        # `tf.nn.top_k` is the core function performing our search.
        # We get scores/labels of shape (batch, beam) with indices in [0..beam_in*dim-1].
        scores, labels = tf.nn.top_k(scores_in_flat, k=beam_size)
        self.search_choices.src_beams = labels // scores_in_dim  # (batch, beam) -> beam_in idx
        labels = labels % scores_in_dim  # (batch, beam) -> dim idx
        labels = tf.reshape(labels, [net_batch_dim * beam_size])  # (batch * beam)
        labels = tf.cast(labels, self.output.dtype)
        self.search_choices.set_beam_scores(scores)  # (batch, beam) -> log score
        self.output = Data(
          name="%s_choice_output" % self.name,
          batch_dim_axis=0,
          shape=self.output.shape,
          sparse=True,
          dim=self.output.dim,
          dtype=self.output.dtype,
          placeholder=labels,
          available_for_inference=True,
          beam_size=beam_size)
    else:
      # Note: If you want to do forwarding, without having the reference,
      # that wont work. You must do search in that case.
      self.output = self._static_get_target_value(
        target=self.target, network=self.network,
        mark_data_key_as_used=True).copy()
      self.output.available_for_inference = True  # in inference, we should do search

  @classmethod
  def get_out_data_from_opts(cls, target, network, beam_size, **kwargs):
    out = cls._static_get_target_value(
      target=target, network=network,
      mark_data_key_as_used=False).copy()
    out.available_for_inference = True  # in inference, we would do search
    if network.search_flag:
      out.beam_size = beam_size
    return out

  # noinspection PyMethodOverriding
  @classmethod
  def get_rec_initial_extra_outputs(cls, network, beam_size, **kwargs):
    """
    :param TFNetwork.TFNetwork network:
    :param int beam_size:
    :rtype: dict[str,tf.Tensor]
    """
    if not network.search_flag:
      return {}
    batch_dim = network.get_batch_dim()
    return {"choice_scores": tf.zeros([batch_dim, beam_size])}


class DecideLayer(LayerBase):
  """
  This is kind of the counter-part to the choice layer.
  This only has an effect in search mode.
  E.g. assume that the input is of shape (batch * beam, time, dim)
  and has search_sources set.
  Then this will output (batch, time, dim) where the beam with the highest score is selected.
  Thus, this will do a decision based on the scores.
  In will convert the data to batch-major mode.
  """
  layer_class = "decide"

  def __init__(self, **kwargs):
    super(DecideLayer, self).__init__(**kwargs)
    # If not in search, this will already be set via self.get_out_data_from_opts().
    if self.network.search_flag:
      assert len(self.sources) == 1
      src = self.sources[0]
      self.decide(src=src, output=self.output)
      self.search_choices = SearchChoices(owner=self, is_decided=True)

  @classmethod
  def decide(cls, src, output=None, name=None):
    """
    :param LayerBase src: with search_choices set. e.g. input of shape (batch * beam, time, dim)
    :param Data|None output:
    :param str|None name:
    :return: best beam selected from input, e.g. shape (batch, time, dim)
    :rtype: Data
    """
    assert src.search_choices
    if not output:
      output = src.output.copy_template(name="%s_output" % (name or src.name)).copy_as_batch_major()
    assert output.batch_dim_axis == 0
    batch_dim = src.network.get_batch_dim()
    src_data = src.output.copy_as_batch_major()
    src_output = tf.reshape(
      src_data.placeholder,
      [batch_dim, src.search_choices.beam_size] +
      [tf.shape(src_data.placeholder)[i] for i in range(1, src_data.batch_ndim)])  # (batch, beam, [time], [dim])
    # beam_scores is of shape (batch, beam) -> log score.
    beam_idxs = tf.argmax(src.search_choices.beam_scores, axis=1)  # (batch,)
    from TFUtil import assert_min_tf_version, nd_indices
    assert_min_tf_version((1, 1), "gather_nd")
    beam_idxs_ext = nd_indices(beam_idxs)
    output.placeholder = tf.cond(
      tf.greater(tf.size(src_output), 0),  # can happen to be empty
      lambda: tf.gather_nd(src_output, indices=beam_idxs_ext),
      lambda: src_output[:, 0])  # (batch, [time], [dim])
    output.size_placeholder = {}
    for i, size in src_data.size_placeholder.items():
      size = tf.reshape(size, [batch_dim, src.search_choices.beam_size])  # (batch, beam)
      output.size_placeholder[i] = tf.gather_nd(size, indices=beam_idxs_ext)  # (batch,)
    return output

  @classmethod
  def get_out_data_from_opts(cls, name, sources, network, **kwargs):
    """
    :param str name:
    :param list[LayerBase] sources:
    :param TFNetwork.TFNetwork network:
    :rtype: Data
    """
    assert len(sources) == 1
    if network.search_flag:
      data = sources[0].output.copy_template(name="%s_output" % name).copy_as_batch_major()
      data.beam_size = None
      return data
    else:
      return sources[0].output


class AttentionBaseLayer(_ConcatInputLayer):
  """
  This is the base class for attention.
  This layer would get constructed in the context of one single decoder step.
  We get the whole encoder output over all encoder frames (the base), e.g. (batch,enc_time,enc_dim),
  and some current decoder context, e.g. (batch,dec_att_dim),
  and we are supposed to return the attention output, e.g. (batch,att_dim).

  Some sources:
  * Bahdanau, Bengio, Montreal, Neural Machine Translation by Jointly Learning to Align and Translate, 2015, https://arxiv.org/abs/1409.0473
  * Luong, Stanford, Effective Approaches to Attention-based Neural Machine Translation, 2015, https://arxiv.org/abs/1508.04025
    -> dot, general, concat, location attention; comparison to Bahdanau
  * https://github.com/ufal/neuralmonkey/blob/master/neuralmonkey/decoders/decoder.py
  * https://google.github.io/seq2seq/
    https://github.com/google/seq2seq/blob/master/seq2seq/contrib/seq2seq/decoder.py
    https://github.com/google/seq2seq/blob/master/seq2seq/decoders/attention_decoder.py
  * https://github.com/deepmind/sonnet/blob/master/sonnet/python/modules/attention.py
  """

  def __init__(self, base, **kwargs):
    """
    :param LayerBase base: encoder output to attend on
    """
    super(AttentionBaseLayer, self).__init__(**kwargs)
    self.base = base
    self.base_weights = None  # type: None|tf.Tensor  # (batch, base_time), see self.get_base_weights()

  def get_dep_layers(self):
    return super(AttentionBaseLayer, self).get_dep_layers() + [self.base]

  def get_base_weights(self):
    """
    We can formulate most attentions as some weighted sum over the base time-axis.

    :return: the weighting of shape (batch, base_time), in case it is defined
    :rtype: tf.Tensor|None
    """
    return self.base_weights

  def get_base_weight_last_frame(self):
    """
    From the base weights (see self.get_base_weights(), must return not None)
    takes the weighting of the last frame in the time-axis (according to sequence lengths).

    :return: shape (batch,) -> float (number 0..1)
    :rtype: tf.Tensor
    """
    last_frame_idxs = tf.maximum(self.base.output.get_sequence_lengths() - 1, 0)  # (batch,)
    from TFUtil import assert_min_tf_version, nd_indices
    assert_min_tf_version((1, 1), "gather_nd")
    last_frame_idxs_ext = nd_indices(last_frame_idxs)
    return tf.gather_nd(self.get_base_weights(), indices=last_frame_idxs_ext)  # (batch,)

  @classmethod
  def transform_config_dict(cls, d, network, get_layer):
    super(AttentionBaseLayer, cls).transform_config_dict(d, network=network, get_layer=get_layer)
    d["base"] = get_layer(d["base"])

  @classmethod
  def get_out_data_from_opts(cls, base, n_out=None, **kwargs):
    """
    :param LayerBase base:
    :rtype: Data
    """
    out = base.output.copy_template_excluding_time_dim()
    assert out.time_dim_axis is None
    if n_out:
      assert out.dim == n_out, (
        "The default attention selects some frame-weighted input of shape [batch, frame, dim=%i]," % out.dim +
        " thus resulting in [batch, dim=%i] but you specified n_out=%i." % (out.dim, n_out))
    return out


class GlobalAttentionContextBaseLayer(AttentionBaseLayer):
  def __init__(self, base_ctx, **kwargs):
    """
    :param LayerBase base_ctx: encoder output used to calculate the attention weights
    """
    super(GlobalAttentionContextBaseLayer, self).__init__(**kwargs)
    self.base_ctx = base_ctx

  def get_dep_layers(self):
    return super(GlobalAttentionContextBaseLayer, self).get_dep_layers() + [self.base_ctx]

  @classmethod
  def transform_config_dict(cls, d, network, get_layer):
    super(GlobalAttentionContextBaseLayer, cls).transform_config_dict(d, network=network, get_layer=get_layer)
    d["base_ctx"] = get_layer(d["base_ctx"])


class DotAttentionLayer(GlobalAttentionContextBaseLayer):
  """
  Classic global attention: Dot-product as similarity measure between base_ctx and source.
  """

  layer_class = "dot_attention"

  def __init__(self, energy_factor=None, **kwargs):
    """
    :param LayerBase base: encoder output to attend on. defines output-dim
    :param LayerBase base_ctx: encoder output used to calculate the attention weights, combined with input-data.
      dim must be equal to input-data
    :param float|None energy_factor: the energy will be scaled by this factor.
      This is like a temperature for the softmax.
      In Attention-is-all-you-need, this is set to 1/sqrt(base_ctx.dim).
    """
    super(DotAttentionLayer, self).__init__(**kwargs)
    # We expect input_data of shape (batch, inner),
    # base_ctx of shape (batch, base_time, inner) and base of shape (batch, base_time, n_out).
    assert self.input_data.batch_ndim == 2
    assert self.input_data.time_dim_axis is None
    assert self.base.output.batch_ndim == 3
    assert self.base.output.dim == self.output.dim
    assert self.base_ctx.output.batch_ndim == 3
    assert self.input_data.dim == self.base_ctx.output.dim
    # And we want to do a dot product so that we get (batch, base_time).
    with tf.name_scope("att_energy"):
      # Get base of shape (batch, base_time, inner).
      base = self.base.output.get_placeholder_as_batch_major()  # (batch, base_time, n_out)
      base_seq_lens = self.base.output.get_sequence_lengths()
      base_ctx = self.base_ctx.output.get_placeholder_as_batch_major()  # (batch, base_time, inner)
      # Get source of shape (batch, inner, 1).
      source = tf.expand_dims(self.input_data.placeholder, axis=2)  # (batch, inner, 1)
      energy = tf.matmul(base_ctx, source)  # (batch, base_time, 1)
      energy.set_shape(tf.TensorShape([None, None, 1]))
      energy = tf.squeeze(energy, axis=2)  # (batch, base_time)
      if energy_factor:
        energy *= energy_factor
      # We must mask all values behind base_seq_lens. Set them to -inf, because we use softmax afterwards.
      energy_mask = tf.sequence_mask(base_seq_lens, maxlen=tf.shape(energy)[1])
      energy = tf.where(energy_mask, energy, float("-inf") * tf.ones_like(energy))
      self.base_weights = tf.nn.softmax(energy)  # (batch, base_time)
      base_weights_bc = tf.expand_dims(self.base_weights, axis=1)  # (batch, 1, base_time)
      out = tf.matmul(base_weights_bc, base)  # (batch, 1, n_out)
      out.set_shape(tf.TensorShape([None, 1, self.output.dim]))
      out = tf.squeeze(out, axis=1)  # (batch, n_out)
      self.output.placeholder = out
      self.output.size_placeholder = {}


class ConcatAttentionLayer(GlobalAttentionContextBaseLayer):
  """
  Additive attention / tanh-concat attention as similarity measure between base_ctx and source.
  This is used by Montreal, where as Stanford compared this to the dot-attention.
  The concat-attention is maybe more standard for machine translation at the moment.
  """

  layer_class = "concat_attention"

  def __init__(self, **kwargs):
    super(ConcatAttentionLayer, self).__init__(**kwargs)
    # We expect input_data of shape (batch, inner),
    # base_ctx of shape (batch, base_time, inner) and base of shape (batch, base_time, n_out).
    assert self.input_data.batch_ndim == 2
    assert self.input_data.time_dim_axis is None
    assert self.base.output.batch_ndim == 3
    assert self.base.output.dim == self.output.dim
    assert self.base_ctx.output.batch_ndim == 3
    assert self.input_data.dim == self.base_ctx.output.dim
    # And we want to get (batch, base_time).
    from TFUtil import expand_multiple_dims
    with tf.name_scope("att_energy"):
      # Get base of shape (batch, base_time, inner).
      base = self.base.output.get_placeholder_as_batch_major()  # (batch, base_time, n_out)
      base_seq_lens = self.base.output.get_sequence_lengths()
      base_ctx = self.base_ctx.output.get_placeholder_as_batch_major()  # (batch, base_time, inner)
      # Get source of shape (batch, inner, 1).
      source = tf.expand_dims(self.input_data.placeholder, axis=1)  # (batch, 1, inner)
      energy_in = tf.tanh(base_ctx + source)  # (batch, base_time, inner)
      energy_weights = self.add_param(tf.get_variable("v", shape=(self.input_data.dim,)))  # (inner,)
      energy_weights_bc = expand_multiple_dims(energy_weights, axes=(0, 1))  # (1, 1, inner)
      energy = tf.reduce_sum(energy_in * energy_weights_bc, axis=2)  # (batch, base_time)
      energy.set_shape(tf.TensorShape([None, None]))
      # We must mask all values behind base_seq_lens. Set them to -inf, because we use softmax afterwards.
      energy_mask = tf.sequence_mask(base_seq_lens, maxlen=tf.shape(energy)[1])
      energy = tf.where(energy_mask, energy, float("-inf") * tf.ones_like(energy))
      self.base_weights = tf.nn.softmax(energy)  # (batch, base_time)
      base_weights_bc = tf.expand_dims(self.base_weights, axis=1)  # (batch, 1, base_time)
      out = tf.matmul(base_weights_bc, base)  # (batch, 1, n_out)
      out.set_shape(tf.TensorShape([None, 1, self.output.dim]))
      out = tf.squeeze(out, axis=1)  # (batch, n_out)
      self.output.placeholder = out
      self.output.size_placeholder = {}


class GaussWindowAttentionLayer(AttentionBaseLayer):
  """
  Interprets the incoming source as the location (float32, shape (batch,))
  and returns a gauss-window-weighting of the base around the location.
  The window size is fixed (TODO: but the variance can optionally be dynamic).
  """

  layer_class = "gauss_window_attention"

  def __init__(self, window_size, std=1., inner_size=None, inner_size_step=0.5, **kwargs):
    """
    :param int window_size: the window size where the Gaussian window will be applied on the base
    :param float std: standard deviation for Gauss
    :param int|None inner_size: if given, the output will have an additional dimension of this size,
      where t is shifted by +/- inner_size_step around.
      e.g. [t-1,t-0.5,t,t+0.5,t+1] would be the locations with inner_size=5 and inner_size_step=0.5.
    :param float inner_size_step: see inner_size above
    """
    super(GaussWindowAttentionLayer, self).__init__(**kwargs)
    from TFUtil import expand_dims_unbroadcast, dimshuffle

    # Code partly adapted from our Theano-based AttentionTimeGauss.
    # The beam is the window around the location center.

    with tf.name_scope("base"):
      base = self.base.output.get_placeholder_as_time_major()  # (base_time,batch,n_in)
    with tf.name_scope("base_seq_lens"):
      base_seq_lens = self.base.output.size_placeholder[0]  # (batch,)
      base_seq_lens_bc = tf.expand_dims(base_seq_lens, axis=0)  # (beam,batch)

    with tf.name_scope("std"):
      # Fixed std for now.
      # std = std_min + a[:, 1] * (std_max - std_min)  # (batch,)
      std = tf.expand_dims(tf.convert_to_tensor(std), axis=0)  # (batch,)

    with tf.name_scope("t"):
      if self.input_data.shape == ():
        t = self.input_data.get_placeholder_as_batch_major()  # (batch,)
      else:
        assert self.input_data.shape == (1,)
        t = tf.squeeze(self.input_data.get_placeholder_as_batch_major(), axis=1)  # (batch,)
      # Now calculate int32 indices for the window.
      t_round = tf.cast(tf.round(t), tf.int32)  # (batch,)
    with tf.name_scope("idxs"):
      start_idxs = t_round - window_size // 2  # (batch,), beams, centered around t_int
      idxs_0 = tf.expand_dims(tf.range(window_size), axis=1)  # (beam,batch). all on cpu, but static, no round trip
      idxs = idxs_0 + tf.expand_dims(start_idxs, axis=0)  # (beam,batch). centered around t_int
    with tf.name_scope("beam"):
      # Handle clipping for idxs.
      cidxs = tf.clip_by_value(idxs, 0, tf.shape(base)[0] - 1)
      cidxs = tf.where(tf.less(cidxs, base_seq_lens_bc), cidxs, tf.ones_like(cidxs) * base_seq_lens_bc - 1)
      # cidxs = tf.Print(cidxs, ["i=", self.network.layers[":i"].output.placeholder, "t=", t, "cidxs=", cidxs[window_size // 2:]])
      # We don't have multi_batch_beam for TF yet.
      # But tf.gather_nd or so might anyway be better to use here.
      # If that will not result in a sparse gradient in the while-loop,
      # some slicing with min(idxs)..max(idxs) might be anther option to at least reduce it a bit.
      # Note that gather_nd is broken up to TF 1.0 for this use case (see test_TFUtil.py),
      # so you need TF >=1.1 here.
      from TFUtil import assert_min_tf_version
      assert_min_tf_version((1, 1), "tf.gather_nd")
      batches_idxs = tf.range(tf.shape(cidxs)[1], dtype=tf.int32, name="batches_idxs")  # (batch,)
      batches_idxs_bc = expand_dims_unbroadcast(batches_idxs, axis=0, dim=tf.shape(cidxs)[0],
                                                name="batches_idxs_bc")  # (beam,batch)
      idxs_exp = tf.stack([cidxs, batches_idxs_bc], axis=2,
                          name="idxs_exp")  # (beam,batch,2), where the 2 stands for (base_time,batch)
      # Thus K == 2. gather_nd out will be idxs_exp.shape[:2] + params.shape[2:] = (beam,batch,n_in).
      gathered = tf.gather_nd(base, idxs_exp)  # (beam,batch,n_in)

    with tf.name_scope("gauss_window"):
      # Gauss window
      idxs_tr_bc = dimshuffle(idxs, (1, 0, 'x'))  # (batch,beam,inner_size)
      std_t_bc = dimshuffle(std, (0, 'x', 'x'))  # (batch,beam,inner_size)
      t_bc = dimshuffle(t, (0, 'x', 'x'))  # (batch,beam,inner_size)
      if inner_size:
        assert isinstance(inner_size, int)
        t_offs = tf.convert_to_tensor(
          [(i * inner_size_step - inner_size / 2.0) for i in range(inner_size)])  # (inner_size,)
        t_offs_bc = dimshuffle(t_offs, ('x', 'x', 0))  # (batch,beam,inner_size)
        t_bc += t_offs_bc
      f_e = tf.exp(-((t_bc - tf.cast(idxs_tr_bc, tf.float32)) ** 2) / (2 * std_t_bc ** 2))  # (batch,beam,inner_size)
      from math import pi, sqrt
      norm = 1. / (std_t_bc * sqrt(2. * pi))  # (batch,beam,inner_size)
      w_t = f_e * norm  # (batch,beam,inner_size)

    with tf.name_scope("att"):
      gathered_tr = dimshuffle(gathered, (1, 2, 'x', 0))  # (batch,n_in,1,beam)
      w_t_bc = expand_dims_unbroadcast(w_t, axis=1, dim=self.base.output.dim)  # (batch,n_in,beam,inner_size)
      att = tf.matmul(gathered_tr, w_t_bc)  # (batch,n_in,1,inner_size)
      att = tf.squeeze(att, axis=2)  # (batch,n_in,inner_size)
      if not inner_size:
        att = tf.squeeze(att, axis=2)  # (batch,n_in)
      else:
        att = tf.transpose(att, (0, 2, 1))  # (batch,inner_size,n_in)

    self.output.placeholder = att
    self.output.size_placeholder = {}

  @classmethod
  def get_out_data_from_opts(cls, inner_size=None, **kwargs):
    out = super(GaussWindowAttentionLayer, cls).get_out_data_from_opts(**kwargs)
    if inner_size:
      assert isinstance(inner_size, int)
      out.shape = out.shape[:-1] + (inner_size,) + out.shape[-1:]
    return out
