import tensorflow as tf
from tensorflow.python.keras import backend as K
from tensorflow.python.keras.engine.base_layer import InputSpec
from tensorflow.python.keras import initializers, regularizers, constraints
from tensorflow.python.keras.layers import Dense, Dropout


class GraphAttentionLayer(Dense):
    """
    import from danielegrattarola/keras-gat
    https://github.com/danielegrattarola/keras-gat/blob/master/keras_gat/graph_attention_layer.py
    """

    def __init__(self,
                 feature_units,
                 attn_heads=1,
                 attn_heads_reduction="concat",  # {"concat", "average"}
                 dropout_rate=0.5,
                 activation="relu",
                 attn_kernel_initializer="glorot_uniform",
                 attn_kernel_regularizer=None,
                 attn_kernel_constraint=None,
                 attention=True,
                 return_attention=False,
                 **kwargs):

        if attn_heads_reduction not in {"concat", "average"}:
            raise ValueError("Possbile reduction methods: concat, average")

        super(GraphAttentionLayer, self).__init__(units=feature_units,
                                                  activation=activation,
                                                  **kwargs)

        # Number of attention heads (K in the paper)
        self.attn_heads = attn_heads
        # Eq. 5 and 6 in the paper
        self.attn_heads_reduction = attn_heads_reduction
        # Internal dropout rate
        self.dropout_rate = dropout_rate

        self.attn_kernel_initializer \
            = initializers.get(attn_kernel_initializer)
        self.attn_kernel_regularizer \
            = regularizers.get(attn_kernel_regularizer)
        self.attn_kernel_constraint = constraints.get(attn_kernel_constraint)
        self.attention = attention
        self.return_attention = return_attention
        self.input_spec = [InputSpec(ndim=3), InputSpec(ndim=3)]
        self.supports_masking = False

        # Populated by build()
        self.kernels = []
        self.biases = []
        self.neighbor_kernels = []
        self.attn_kernels = []
        self.attention_biases = []

        if attn_heads_reduction == "concat":
            # Output will have shape (..., K * F")
            self.output_dim = self.units * self.attn_heads
        else:
            # Output will have shape (..., F")
            self.output_dim = self.units

    def build(self, input_shape):
        X_dims, A_dims = [dims.as_list() for dims in input_shape]
        assert len(X_dims) == 3
        assert len(A_dims) == 3 and A_dims[1] == A_dims[2]

        _, N, F = X_dims

        # Initialize weights for each attention head
        for head in range(self.attn_heads):
            # Layer kernel
            kernel = self.add_weight(shape=(F, self.units),
                                     initializer=self.kernel_initializer,
                                     regularizer=self.kernel_regularizer,
                                     constraint=self.kernel_constraint,
                                     name="kernel_{}".format(head))
            self.kernels.append(kernel)

            # Layer bias
            if self.use_bias:
                bias = self.add_weight(shape=(self.units,),
                                       initializer=self.bias_initializer,
                                       regularizer=self.bias_regularizer,
                                       constraint=self.bias_constraint,
                                       name="bias_{}".format(head))
                self.biases.append(bias)

            if not self.attention:
                continue

            # Attention kernels
            neighbor_kernel = self.add_weight(
                                    shape=(F, self.units),
                                    initializer=self.kernel_initializer,
                                    regularizer=self.kernel_regularizer,
                                    constraint=self.kernel_constraint,
                                    name="kernel_neighbor_{}".format(head))

            attn_kernel = self.add_weight(
                                    shape=(self.units, 1),
                                    initializer=self.attn_kernel_initializer,
                                    regularizer=self.attn_kernel_regularizer,
                                    constraint=self.attn_kernel_constraint,
                                    name="attn_kernel_{}".format(head))

            self.neighbor_kernels.append(neighbor_kernel)
            self.attn_kernels.append(attn_kernel)

            if self.use_bias:
                """
                bias = self.add_weight(shape=(1, ),
                                       initializer=self.bias_initializer,
                                       regularizer=self.bias_regularizer,
                                       constraint=self.bias_constraint,
                                       name="bias_attn_{}".format(head))
                self.attention_biases.append(bias)
                """
                pass

        self.built = True

    def call(self, inputs):
        X = inputs[0]  # Node features (B x N x F)
        A = inputs[1]  # Adjacency matrix (B x N x N)

        X_dims = X.get_shape().as_list()
        B, N, F = X_dims

        outputs = []
        attentions = []
        for head in range(self.attn_heads):
            # W in the paper (F x F")
            kernel = self.kernels[head]

            # Compute inputs to attention network
            features = K.dot(X, kernel)  # (B x N x F")
            dropout_feat = Dropout(self.dropout_rate)(features)  # (B x N x F")

            if not self.attention:
                attention = A
                node_features = tf.matmul(attention, dropout_feat)  # (N x F")
            else:
                # Attention kernel a in the paper (2F" x 1)
                neighbor_kernel = self.neighbor_kernels[head]
                attention_kernel = self.attn_kernels[head]
                # attention_bias = self.attention_biases[head]

                neighbor_features = K.dot(X, neighbor_kernel)

                feature_self = K.repeat_elements(features, N, axis=2)
                feature_self = K.reshape(feature_self, (-1, N, N, self.units))

                feature_neighbor = K.repeat_elements(neighbor_features, N, axis=2)
                feature_neighbor = K.reshape(feature_neighbor, (-1, N, N, self.units))

                merged = feature_self + tf.transpose(feature_neighbor, (0, 2, 1, 3))
                attention = K.dot(tf.nn.tanh(merged), attention_kernel)
                attention = K.reshape(attention, (-1, N, N))
                if self.use_bias:
                    # attention = K.bias_add(attention, attention_bias)
                    pass

                mask = -10e9 * (1.0 - A)
                attention += mask

                attention = tf.nn.softmax(attention)
                dropout_attn = Dropout(self.dropout_rate)(attention)

                node_features = tf.matmul(dropout_attn, dropout_feat)

            if self.use_bias:
                node_features = K.bias_add(node_features, self.biases[head])

            if self.return_attention:
                attentions.append(attention)
            # Add output of attention head to final output
            outputs.append(node_features)

        # Aggregate the heads" output according to the reduction method
        if self.attn_heads_reduction == "concat":
            output = K.concatenate(outputs, axis=-1)  # (B x N x KF")
        else:
            output = K.mean(K.stack(outputs), axis=0)  # (B x N x F")
            # If "average", compute the activation here (Eq. 6)

        output = self.activation(output)

        if self.return_attention:
            attentions = K.stack(attentions, axis=1)
            return (output, attentions)
        else:
            return output

    def compute_output_shape(self, input_shape):
        X_dims, A_dims = [dims.as_list() for dims in input_shape]
        assert len(X_dims) == 3
        assert len(A_dims) == 3
        output_shape = X_dims[0], X_dims[0], self.output_dim

        if self.return_attention:
            return (tf.TensorShape(output_shape),
                    tf.TensorShape(A_dims.insert(1, self.attn_heads)))
        else:
            return tf.TensorShape(output_shape)
