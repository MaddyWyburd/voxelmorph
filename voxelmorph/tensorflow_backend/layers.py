import neuron as ne
import tensorflow as tf
from tensorflow import keras as keras
import tensorflow.keras.backend as K
from tensorflow.keras.layers import Layer


# make the following neuron layers directly available from vxm
SpatialTransformer = ne.layers.SpatialTransformer
LocalParam = ne.layers.LocalParam


class Rescale(Layer):
    """ 
    Rescales a layer by some factor.
    """

    def __init__(self, scale_factor, **kwargs):
        self.scale_factor = scale_factor
        super().__init__(**kwargs)

    def build(self, input_shape):
        super().build(input_shape)

    def call(self, x):
        return x * self.scale_factor

    def compute_output_shape(self, input_shape):
        return input_shape


class RescaleTransform(Layer):
    """ 
    Rescales a transform, which involves resizing the vector field *and* rescaling it.
    """

    def __init__(self, zoom_factor, interp_method='linear', **kwargs):
        self.zoom_factor = zoom_factor
        self.interp_method = interp_method
        super().__init__(**kwargs)

    def build(self, input_shape):

        if isinstance(input_shape[0], (list, tuple)) and len(input_shape) > 1:
            raise Exception('RescaleTransform must be called on a list of length 1.')

        if isinstance(input_shape[0], (list, tuple)):
            input_shape = input_shape[0]

        super().build(input_shape)

    def call(self, inputs):

        # check shapes
        if isinstance(inputs, (list, tuple)):
            assert len(inputs) == 1, "inputs has to be len 1. found: %d" % len(inputs)
            trf = inputs[0]
        else:
            trf = inputs

        if self.zoom_factor < 1:
            # resize
            trf = ne.layers.Resize(self.zoom_factor, name=self.name + '_resize')(trf)
            return Rescale(self.zoom_factor, name=self.name + '_rescale')(trf)
        else:
            # multiply first to save memory (multiply in smaller space)
            trf = Rescale(self.zoom_factor, name=self.name + '_rescale')(trf)
            return ne.layers.Resize(self.zoom_factor, name=self.name + '_resize')(trf)

    def compute_output_shape(self, input_shape):
        output_shape = [int(dim * self.zoom_factor) for dim in input_shape[1:-1]]
        output_shape = [input_shape[0]] + output_shape + [input_shape[-1]]
        return tuple(output_shape)


class ComposeTransform(Layer):
    """ 
    Composes two dense deformations specified by their displacements.

    We have two fields:

    A --> B (so field is in space of B)
    B --> C (so field is in the space of C)
    
    This layer composes a new warp field:

    A --> C (so field is in the space of C)
    """

    def build(self, input_shape):

        if len(input_shape) != 2:
            raise Exception('ComposeTransform must be called on a input list of length 2.')

        super().build(input_shape)

    def call(self, inputs):
        """
        Parameters
            inputs: list with two dense deformations
        """
        assert len(inputs) == 2, "inputs has to be len 2, found: %d" % len(inputs)
        return tf.map_fn(self._single_compose, inputs, dtype=tf.float32)

    def _single_compose(self, inputs):
        return ne.utils.compose(inputs[0], inputs[1])

    def compute_output_shape(self, input_shape):
        return input_shape


class LocalParamWithInput(Layer):
    """ 
    Update 9/29/2019 - TODO: should try ne.layers.LocalParam() again after update.

    The neuron.layers.LocalParam has an issue where _keras_shape gets lost upon calling get_output :(

    tried using call() but this requires an input (or i don't know how to fix it)
    the fix was that after the return, for every time that tensor would be used i would need to do something like
    new_vec._keras_shape = old_vec._keras_shape

    which messed up the code. Instead, we'll do this quick version where we need an input, but we'll ignore it.

    this doesn't have the _keras_shape issue since we built on the input and use call()
    """

    def __init__(self, shape, initializer='RandomNormal', mult=1.0, **kwargs):
        self.shape = shape
        self.initializer = initializer
        self.biasmult = mult
        print('LocalParamWithInput: Consider using neuron.layers.LocalParam()')
        super().__init__(**kwargs)

    def build(self, input_shape):
        self.kernel = self.add_weight(name='kernel', 
                                      shape=self.shape,  # input_shape[1:]
                                      initializer=self.initializer,
                                      trainable=True)
        super().build(input_shape)  # Be sure to call this somewhere!

    def call(self, x):
        # want the x variable for it's keras properties and the batch.
        b = 0 * K.batch_flatten(x)[:, 0:1] + 1
        params = K.expand_dims(K.flatten(self.kernel * self.biasmult), 0)
        z = K.reshape(K.dot(b, params), [-1, *self.shape])
        return z

    def compute_output_shape(self, input_shape):
        return (input_shape[0], *self.shape)


class EulerToAffine(Layer):
    """
    Computes the corresponding (flattened) affine from a 6-element
    vector where the first 3 values represent the x, y, z euler angles
    (in radians) and the last 3 represent the x, y, z translation.

    TODO: make this dimension-independent
    """

    def build(self, input_shape):

        if input_shape[1] != 6:
            raise NotImplementedError('Rigid registration is limited to 3D (len 6 input) for now')

        super().build(input_shape)

    def compute_output_shape(self, input_shape):
        output_shape = input_shape
        output_shape[1] = 12
        return output_shape

    def call(self, vector):
        """
        Parameters
            vector: tensor of ndim euler angles and ndim translations.
        """
        return tf.map_fn(self._single_euler_to_affine, vector, dtype=tf.float32)

    def _single_euler_to_affine(self, vector):

        # extract components of input vector
        angle_x = vector[0]
        angle_y = vector[1]
        angle_z = vector[2]
        translation = tf.expand_dims(vector[3:6], 1)

        # x rotation matrix
        cosx  = tf.math.cos(angle_x)
        sinx  = tf.math.sin(angle_x)
        x_rot = tf.convert_to_tensor([
            [1,    0,     0],
            [0, cosx, -sinx],
            [0, sinx,  cosx]
        ], name='x_rot')

        # y rotation matrix
        cosy  = tf.math.cos(angle_y)
        siny  = tf.math.sin(angle_y)
        y_rot = tf.convert_to_tensor([
            [cosy,  0, siny],
            [0,     1,    0],
            [-siny, 0, cosy]
        ], name='y_rot')

        # z rotation matrix
        cosz  = tf.math.cos(angle_z)
        sinz  = tf.math.sin(angle_z)
        z_rot = tf.convert_to_tensor([
            [cosz, -sinz, 0],
            [sinz,  cosz, 0],
            [0,        0, 1]
        ], name='z_rot')

        # compose matrices
        t_rot = tf.tensordot(x_rot, y_rot, 1)
        m_rot = tf.tensordot(t_rot, z_rot, 1)

        # concat the linear translation and flatten
        matrix = tf.concat([m_rot, translation], 1)
        affine = tf.reshape(matrix, [12])

        return affine