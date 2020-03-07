import math
import os
from datetime import datetime

import click
import tensorflow as tf

from hourglass104 import StackedHourglassNetwork
from preprocess import Preprocessor

IMAGE_SHAPE = (256, 256, 3)
BATCH_SIZE = 32
HEATMAP_SHAPE = (64, 64, 16)
TF_RECORDS_DIR = './dataset/tfrecords_mpii/'


class Trainer(object):
    def __init__(self,
                 model,
                 epochs,
                 global_batch_size,
                 strategy,
                 initial_learning_rate=0.00025,
                 start_epoch=1):
        self.start_epoch = start_epoch
        self.model = model
        self.epochs = epochs
        self.strategy = strategy
        self.global_batch_size = global_batch_size
        self.loss_object = tf.keras.losses.MeanSquaredError(
            reduction=tf.keras.losses.Reduction.NONE)
        # "we use rmsprop with a learning rate of 2.5e-4.""
        self.optimizer = tf.keras.optimizers.Adam(
            learning_rate=initial_learning_rate)
        self.model = model
        
        self.current_learning_rate = initial_learning_rate
        self.last_val_loss = math.inf
        self.lowest_val_loss = math.inf
        self.patience_count = 0
        self.max_patience = 5
        self.tensorboard_dir = './logs/'

    def lr_decay(self):
        """
        This effectively simulate ReduceOnPlateau learning rate schedule. Learning rate
        will be reduced by a factor of 5 if there's no improvement over [max_patience] epochs
        """
        if self.patience_count >= self.max_patience:
            self.current_learning_rate /= 5.0
            self.patience_count = 0
        elif self.last_val_loss == self.lowest_val_loss:
            self.patience_count = 0
        self.patience_count += 1

        self.optimizer.learning_rate = self.current_learning_rate

    def compute_loss(self, labels, outputs):
        loss = 0
        for output in outputs:
            loss += tf.reduce_sum(self.loss_object(
                labels, output)) * (1. / self.global_batch_size)
        return loss

    def train_step(self, inputs):
        images, labels = inputs
        with tf.GradientTape() as tape:
            outputs = self.model(images, training=True)
            loss = self.compute_loss(labels, outputs)

        grads = tape.gradient(
            target=loss, sources=self.model.trainable_variables)
        self.optimizer.apply_gradients(
            zip(grads, self.model.trainable_variables))

        return loss

    def val_step(self, inputs):
        images, labels = inputs
        logits = self.model(images, training=False)
        loss = self.compute_loss(labels, logits)
        return loss

    def run(self, train_dist_dataset, val_dist_dataset):
        @tf.function
        def distributed_train_epoch(dataset):
            total_loss = 0.0
            num_train_batches = 0.0
            for one_batch in dataset:
                per_replica_loss = self.strategy.experimental_run_v2(
                    self.train_step, args=(one_batch, ))
                batch_loss = self.strategy.reduce(
                    tf.distribute.ReduceOp.SUM, per_replica_loss, axis=None)
                total_loss += batch_loss
                num_train_batches += 1
                tf.print('Trained batch', num_train_batches, 'batch loss',
                         batch_loss, 'epoch total loss', total_loss)
            return total_loss, num_train_batches

        @tf.function
        def distributed_val_epoch(dataset):
            total_loss = 0.0
            num_val_batches = 0.0
            for one_batch in dataset:
                per_replica_loss = self.strategy.experimental_run_v2(
                    self.val_step, args=(one_batch, ))
                num_val_batches += 1
                batch_loss = self.strategy.reduce(
                    tf.distribute.ReduceOp.SUM, per_replica_loss, axis=None)
                total_loss += batch_loss
            return total_loss, num_val_batches
        
        summary_writer = tf.summary.create_file_writer(self.tensorboard_dir)

        for epoch in range(self.start_epoch, self.epochs + 1):
            self.lr_decay()
            print('Start epoch {} with learning rate {}'.format(epoch, self.current_learning_rate))

            train_total_loss, num_train_batches = distributed_train_epoch(
                train_dist_dataset)
            train_loss = train_total_loss / num_train_batches
            print('Epoch {} train loss {}'.format(
                epoch, train_loss))
            with summary_writer.as_default():
                tf.summary.scalar('epoch train loss', train_loss, step=epoch)

            val_total_loss, num_val_batches = distributed_val_epoch(
                val_dist_dataset)
            val_loss = val_total_loss / num_val_batches
            print('Epoch {} val loss {}'.format(
                epoch, val_loss))
            with summary_writer.as_default():
                tf.summary.scalar('epoch val loss', val_loss, step=epoch)

            # save model when reach a new lowest validation loss
            if val_loss < self.lowest_val_loss:
                self.save_model(epoch, val_loss)
                self.lowest_val_loss = val_loss
            self.last_val_loss = val_loss

        self.save_model(self.epochs, self.last_val_loss)
        
    def save_model(self, epoch, loss):
        model_name = './models/model-v1.0.0-epoch-{}-loss-{:.4f}.h5'.format(
            epoch, loss)
        self.model.save_weights(model_name)
        print("Model {} saved.".format(model_name))


def create_dataset(tfrecords, batch_size, is_train):
    preprocess = Preprocessor(IMAGE_SHAPE, HEATMAP_SHAPE, is_train)

    dataset = tf.data.Dataset.list_files(tfrecords)
    dataset = tf.data.TFRecordDataset(dataset)
    dataset = dataset.map(preprocess, num_parallel_calls=8)

    if is_train:
        dataset = dataset.shuffle(128)

    dataset = dataset.batch(batch_size)
    dataset = dataset.prefetch(buffer_size=batch_size)

    return dataset


@click.command()
@click.option('--epochs', default=120, help='Total number of epochs.')
@click.option('--start_epoch', default=1, help='The epoch number to start with.')
@click.option('--checkpoint', help='The path to checkpoint file.')
@click.option('--learning_rate', default=0.00025, help='The learning rate to start with.')
def main(epochs, start_epoch, learning_rate, checkpoint):
    strategy = tf.distribute.MirroredStrategy()
    global_batch_size = strategy.num_replicas_in_sync * BATCH_SIZE
    train_dataset = create_dataset(
        os.path.join(TF_RECORDS_DIR, 'train*'),
        global_batch_size,
        is_train=True)
    val_dataset = create_dataset(
        os.path.join(TF_RECORDS_DIR, 'val*'),
        global_batch_size,
        is_train=False)
    
    if not os.path.exists(os.path.join('./models')):
        os.makedirs(os.path.join('./models/'))

    with strategy.scope():
        train_dist_dataset = strategy.experimental_distribute_dataset(
            train_dataset)
        val_dist_dataset = strategy.experimental_distribute_dataset(
            val_dataset)

        model = StackedHourglassNetwork(IMAGE_SHAPE, 4, 1, HEATMAP_SHAPE[2])
        if checkpoint:
            model.load_weights(checkpoint)
        # model.summary()

        trainer = Trainer(model, epochs, global_batch_size, strategy,
                          initial_learning_rate=learning_rate, start_epoch=start_epoch)

        print('Start training...')
        trainer.run(train_dist_dataset, val_dist_dataset)


if __name__ == "__main__":
    main()