# Copyright 2019 Purify Foundation
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

# Fine-tuning ImageNet trained InceptionV3

# %%
import os
from time import time

import tensorflow as tf
from . import project_path
from . import image_preprocessing
from . import distribution_utils

# Consts
IMG_SIZE = 299
OUTPUT_CLASSES_NUM = 3
OUTPUT_PREFIX = "Geacc_InceptionV3_"
OUTPUT_ID = ""
TB_SUMMARY = "summary"
HPARAMS = {}


def get_filenames(data_subset):
    """Get TFRecord filenames for dataset.

    Args:
        data_subset: Dataset type "train", "validate" or "test"
    """
    filenames = [
        os.path.join(HPARAMS['dataset_path'], '{}-{:05d}-of-{:05d}'.format(data_subset, i, HPARAMS[f'{data_subset}_tfrecord_files']))
            for i in range(HPARAMS[f'{data_subset}_tfrecord_files'])]

    if HPARAMS['tpu_address'] and not distribution_utils.tpu_compatible_files(filenames):
        raise Exception("TPU requires files stored in Google Cloud Storage (GCS) buckets.")

    return filenames


def step_decay_schedule(epoch):
    '''Slow down training deeper into dataset.'''
    if epoch < 2:
        # Warmup model first
        return .0000032
    elif epoch < 8:
        return .01
    elif epoch < 16:
        return .002
    elif epoch < 32:
        return .0004
    elif epoch < 64:
        return .00008
    elif epoch < 96:
        return .000016
    else:
        return .0000009


def get_optimizer():
    """Returns optimizer.

    Raises:
      ValueError: if type of optimizer specified in hparams is incorrect.

    Returns:
      Instance of optimizer class.
    """
    if HPARAMS['optimizer'] == 'sgd':
        optimizer = tf.keras.optimizers.SGD(momentum=HPARAMS['momentum'])
    elif HPARAMS['optimizer'] == 'adam':
        optimizer = tf.keras.optimizers.Adam(lr=HPARAMS['learning_rate'])
    else:
        raise ValueError('Invalid value of optimizer: %s' % HPARAMS['optimizer'])
    return optimizer


def init_callbacks():
    """Setup callbacks."""
    use_callbacks = []

    # Checkpoint
    checkpoint_file = os.path.join(HPARAMS['models_path'], OUTPUT_PREFIX + OUTPUT_ID + "_ep{epoch:02d}_vl{val_loss:.2f}.tf")
    checkpointer = tf.keras.callbacks.ModelCheckpoint(filepath=checkpoint_file, monitor='val_loss', verbose=1, save_best_only=False)
    use_callbacks.append(checkpointer)

    # Early stopping
    # early_stop = tf.keras.callbacks.EarlyStopping(monitor='val_loss', min_delta=0.001, patience=10)
    # use_callbacks.append(early_stop)

    # learning rate schedule
    if HPARAMS['optimizer'] == 'sgd':
        lr_scheduler = tf.keras.callbacks.LearningRateScheduler(step_decay_schedule)
        use_callbacks.append(lr_scheduler)

    # Tensorboard logs
    if (HPARAMS['tb_path']):
        tb_logs_dir = os.path.join(HPARAMS['tb_path'], OUTPUT_PREFIX + OUTPUT_ID)
        print('TensorBoard events:', tb_logs_dir)

        # Capture hyperparameters in Tensorboard
        with tf.summary.create_file_writer(tb_logs_dir + "/" + TB_SUMMARY).as_default():
            tensor = tf.stack([tf.convert_to_tensor([k, str(v)]) for k, v in HPARAMS.items()])
            tf.summary.text("Hyperparameters", tensor, step=0)

        tensorboard = tf.keras.callbacks.TensorBoard(log_dir=tb_logs_dir)
        use_callbacks.append(tensorboard)

    return use_callbacks


def build_model():
    '''Build InceptionV3 model.'''
    tf.keras.backend.clear_session()

    pretrained_model = tf.keras.applications.InceptionV3(
        weights='imagenet',
        include_top=False, 
        pooling='avg',
        input_shape=(IMG_SIZE, IMG_SIZE, 3)
    )

    # Freeze bottom N layers
    for layer in pretrained_model.layers[:HPARAMS['frozen_layer_num']]:
        layer.trainable = False

    model = tf.keras.Sequential([
        pretrained_model,
        tf.keras.layers.Dense(OUTPUT_CLASSES_NUM, activation='softmax', name='predictions')
    ])

    return model


def optimize_performance():
    # Enable XLA
    if HPARAMS['enable_xla']:
        tf.config.optimizer.set_jit(True)

    # Use mixed precision when available
    if HPARAMS['enable_mixed_precision']:
        if HPARAMS['tpu_address']:
            policy = 'mixed_bfloat16'
            dtype = tf.bfloat16
        elif HPARAMS['gpu_num']:
            policy = 'mixed_float16'
            dtype = tf.float16
        else:
            policy = 'float32'
            dtype = tf.float32

        print(f"Setting mixed precision policy to {policy}.")
        tf.keras.mixed_precision.experimental.set_policy(policy)
        HPARAMS['mixed_precision_policy'] = policy
        HPARAMS['dtype'] = dtype


def load_datasets():
    '''Prepare images for training.'''
    train_input_dataset = image_preprocessing.input_fn(
        is_training = True,
        filenames   = get_filenames(data_subset="train"),
        batch_size  = HPARAMS['batch_size'],
        num_epochs  = HPARAMS['total_epochs'],
        parse_record_fn = image_preprocessing.get_parse_record_fn(one_hot_encoding_class_num=OUTPUT_CLASSES_NUM),
        # datasets_num_private_threads=None,
        dtype=HPARAMS['dtype'],
        drop_remainder=HPARAMS['enable_xla'],
        # tf_data_experimental_slack=False,
        # training_dataset_cache=False,
    )

    validate_input_dataset = image_preprocessing.input_fn(
        is_training = False,
        filenames   = get_filenames(data_subset="validate"),
        batch_size  = HPARAMS['batch_size'],
        num_epochs  = HPARAMS['total_epochs'],
        parse_record_fn = image_preprocessing.get_parse_record_fn(one_hot_encoding_class_num=OUTPUT_CLASSES_NUM),
        dtype=HPARAMS['dtype'],
        drop_remainder=HPARAMS['enable_xla'],
    )

    class_names = HPARAMS['class_names']
    print(f"Class names: {class_names}")

    steps_train = HPARAMS['train_image_files'] // HPARAMS['batch_size']
    print(f"Steps on train: {steps_train}")

    steps_validate = HPARAMS['validate_image_files'] // HPARAMS['batch_size']
    print(f"Steps on validation: {steps_validate}")

    return [train_input_dataset, validate_input_dataset, steps_train, steps_validate]


def load_checkpoint(checkpoint_file, model):
    if os.path.exists(checkpoint_file):
        print("Resuming from checkpoint: ", checkpoint_file)
        model.load_weights(checkpoint_file)

        # Finding the epoch index from which we are resuming
        # initial_epoch = get_init_epoch(checkpoint_path)

        # Calculating the correct value of count
        # count = initial_epoch*batches_per_epoch


def train():
    """Run InceptionV3 training."""

    optimize_performance()

    strategy = distribution_utils.get_distribution_strategy(
        distribution_strategy=HPARAMS['distribution_strategy'],
        num_gpus=HPARAMS['gpu_num'],
        tpu_address=HPARAMS['tpu_address'])

    strategy_scope = distribution_utils.get_strategy_scope(strategy)

    # Get training/validation datasets
    train_input_dataset, validate_input_dataset, steps_train, steps_validate = load_datasets()

    with strategy_scope:
        model = build_model()
        model.summary()

        model.compile(
            optimizer=get_optimizer(),
            loss=tf.losses.CategoricalCrossentropy(),
            metrics=[tf.metrics.CategoricalAccuracy(), tf.metrics.AUC(), tf.metrics.Precision(), tf.metrics.Recall()]
        )

    print('Starting training')

    model.fit(
        train_input_dataset,
        steps_per_epoch=steps_train,
        epochs=HPARAMS['total_epochs'],
        validation_data=validate_input_dataset,
        validation_steps=steps_validate,
        callbacks=init_callbacks(),
        # verbose=2
    )

    # Save final model
    model.save(os.path.join(HPARAMS['models_path'], OUTPUT_PREFIX + OUTPUT_ID + "_final.tf"))

    return model


def eval(model):
    """Run InceptionV3 model evaluation."""

    print("\nEvaluating final model using test dataset...")

    test_input_dataset = image_preprocessing.input_fn(
        is_training = False,
        filenames   = get_filenames(data_subset="test"),
        batch_size  = HPARAMS['batch_size'],
        num_epochs  = HPARAMS['total_epochs'],
        parse_record_fn = image_preprocessing.get_parse_record_fn(one_hot_encoding_class_num=OUTPUT_CLASSES_NUM),
        dtype=HPARAMS['dtype'],
        drop_remainder=HPARAMS['enable_xla'],
    )

    steps_test = HPARAMS['test_image_files'] // HPARAMS['batch_size']
    print(f"Steps on test: {steps_test}")

    eval_output = model.evaluate(
        test_input_dataset,
        steps=steps_test,
        # verbose=2
    )

    eval_stats = {
        'loss': float(eval_output[0]),
        'accuracy': float(eval_output[1])
    }

    print("Evaluation results for test dataset: Loss={0:.4f} Accuracy={1:.4f}".format(eval_stats['loss'], eval_stats['accuracy']))

    # Capture results in Tensorboard
    if (HPARAMS['tb_path']):
        tb_logs_dir = os.path.join(HPARAMS['tb_path'], OUTPUT_PREFIX + OUTPUT_ID)
        with tf.summary.create_file_writer(tb_logs_dir + "/" + TB_SUMMARY).as_default():
            tensor = tf.stack([tf.convert_to_tensor([k, str(v)]) for k, v in eval_stats.items()])
            tf.summary.text("Evaluation Results (Test dataset)", tensor, step=HPARAMS['total_epochs'] + 1)


def run(hparams=None):
    """InveptionV3

    Args:
        hparams: Model hyperparameters and other arguments
    """

    global HPARAMS
    HPARAMS = hparams

    global OUTPUT_ID
    OUTPUT_ID = str(int(time()))

    model = train()
    eval(model)