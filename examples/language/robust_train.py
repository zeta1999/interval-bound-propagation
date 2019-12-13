# coding=utf-8
# Copyright 2019 The Interval Bound Propagation Authors.
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

"""Train sentence representation models on individual tasks."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import imp
import json
import os

from absl import app
from absl import flags
from absl import logging
from interval_bound_propagation.examples.language import robust_model
import numpy as np
from six.moves import range
import tensorflow as tf


if __name__ == '__main__':
  flags.DEFINE_string('config_path', './config.py',
                      'Path to training configuration file.')
  flags.DEFINE_integer('batch_size', 30, 'Batch size.')
  flags.DEFINE_integer('num_train_steps', 5000, 'Number of training steps.')
  flags.DEFINE_integer('num_oov_buckets', 1, 'Number of out-of-vocab buckets.')
  flags.DEFINE_integer('report_every', 100,
                       'Report test loss every N batches.')
  flags.DEFINE_float('schedule_ratio', 0.3, 'schedule_ratio * num_train_steps'
                     '= the steps where reach the final delta/dual_loss.')
  flags.DEFINE_float('learning_rate', 0.001, 'Learning rate.')
  flags.DEFINE_float('max_grad_norm', 5.0, 'Maximum norm of gradients.')
  flags.DEFINE_boolean('fine_tune_embeddings', True, 'Finetune embeddings.')
  flags.DEFINE_string('task', 'sst', 'One of snli, mnli, sick, sst.')
  flags.DEFINE_string('pooling', 'average', 'One of averge, sum, max, last.')
  flags.DEFINE_boolean('evaluate', False, 'Evaluate mode.')
  flags.DEFINE_boolean('analysis', False, 'Analysis mode.')
  flags.DEFINE_string('analysis_split', 'test', 'Analysis dataset split.')
  flags.DEFINE_string('experiment_root',
                      '/tmp/robust_model/',
                      'Path to save trained models.')
  flags.DEFINE_string('tb_dir', None,
                      'TB folder. If not specified, set under experiment_root')
  FLAGS = flags.FLAGS


def load_synonyms(synonym_filepath=None):
  synonyms = None
  with open(synonym_filepath) as f:
    synonyms = json.load(f)
  return synonyms


def construct_synonyms(synonym_filepath):
  synonyms = load_synonyms(synonym_filepath)
  synonym_keys = list(synonyms.keys())
  synonym_values = [synonyms[k] for k in synonym_keys]
  max_synoynm_counts = max([len(s) for s in synonym_values])
  synonym_value_lens = [len(x) for x in synonym_values]
  # Add 0 for the first starting point.
  synonym_value_lens_cum = np.cumsum([0] + synonym_value_lens)
  synonym_values_list = [word for val in synonym_values for word in val]  # pylint: disable=g-complex-comprehension
  return synonym_keys, max_synoynm_counts, synonym_value_lens_cum, synonym_values_list


def linear_schedule(step, init_step, final_step, init_value, final_value):
  """Linear schedule."""
  assert final_step >= init_step
  if init_step == final_step:
    return final_value
  rate = np.float32(step - init_step) / float(final_step - init_step)
  linear_value = rate * (final_value - init_value) + init_value
  return np.clip(linear_value, min(init_value, final_value),
                 max(init_value, final_value))


def config_train_summary(task, train_accuracy, loss):
  """Add ops for summary in the computation graph.

  Args:
    task: string name of task being trained for.
    train_accuracy: training accuracy.
    loss: training loss.

  Returns:
    train_summ: summary for training.
    saver: tf.saver, used to save the checkpoint with the best dev accuracy.
  """
  train_acc_summ = tf.summary.scalar(('%s_train_accuracy' % task),
                                     train_accuracy)
  loss_summ = tf.summary.scalar('loss', loss)
  train_summ = tf.summary.merge([train_acc_summ, loss_summ])
  return train_summ


def train(config_dict, synonym_filepath,
          batch_size, num_train_steps, schedule_ratio, report_every,
          snapshot_path, tb_dir):
  """Model training."""
  graph_tensor_producer = robust_model.RobustModel(**config_dict)
  graph_tensors = graph_tensor_producer()

  synonym_keys, max_synoynm_counts, synonym_value_lens_cum, \
      synonym_values_list = construct_synonyms(synonym_filepath)
  train_summ = config_train_summary(config_dict['task'],
                                    graph_tensors['train_accuracy'],
                                    graph_tensors['loss'])

  tf.gfile.MakeDirs(snapshot_path)

  best_dev_accuracy = 0.0
  best_test_accuracy = 0.0
  best_verified_dev_accuracy = 0.0
  best_verified_test_accuracy = 0.0

  network_saver = tf.train.Saver(graph_tensor_producer.variables)
  with tf.train.SingularMonitoredSession() as session:
    logging.info('Initialize parameters...')
    writer = tf.summary.FileWriter(tb_dir, session.graph)
    input_feed = {}

    # Tokenize synonyms
    tokenize_synonyms = [[] for _ in range(graph_tensors['vocab_size'])]
    lookup_indices_keys = session.run(graph_tensors['indices'],
                                      feed_dict={graph_tensors['lookup_token']:
                                                 synonym_keys})
    lookup_indices_values = session.run(graph_tensors['indices'],
                                        feed_dict={
                                            graph_tensors['lookup_token']:
                                            synonym_values_list})
    for i, key_index in enumerate(lookup_indices_keys):
      tokenize_synonyms[key_index] = lookup_indices_values[
          synonym_value_lens_cum[i]:synonym_value_lens_cum[i+1]].tolist()

    synonym_values_np = np.zeros([graph_tensors['vocab_size'],
                                  max_synoynm_counts])
    for i in range(graph_tensors['vocab_size']):
      # False-safe case. No perturbations. Set it as itself.
      synonym_values_np[i][0] = i
      for j in range(len(tokenize_synonyms[i])):
        synonym_values_np[i][j] = tokenize_synonyms[i][j]
    synonym_counts_np = [len(s) for s in tokenize_synonyms]
    input_feed[graph_tensors['synonym_values']] = synonym_values_np
    input_feed[graph_tensors['synonym_counts']] = synonym_counts_np

    warmup_steps = 0
    for step in range(num_train_steps):
      config = config_dict['config']
      if config['delta'] > 0.0 and config['delta_schedule']:
        delta = linear_schedule(
            step, 0., schedule_ratio * num_train_steps,
            0., config['delta'])
        input_feed[graph_tensors['delta']] = delta

      if config['dual_loss_ratio'] > 0.0 and config['dual_loss_schedule']:
        if delta > 0.0 and warmup_steps == 0:
          warmup_steps = step
        if delta > 0.0:
          dual_loss_ratio = linear_schedule(
              step, warmup_steps, schedule_ratio * num_train_steps,
              0., config['dual_loss_ratio'])
        else:
          dual_loss_ratio = 0.0
        input_feed[graph_tensors['dual_loss_ratio']] = dual_loss_ratio

      total_loss_np, loss_np, dual_loss_np, train_accuracy_np, \
          train_bound, train_verified, \
          dual_loss_ratio_val, delta_val, \
          train_summ_py, _ = session.run(
              [graph_tensors['total_loss'],
               graph_tensors['loss'],
               graph_tensors['dual_loss'],
               graph_tensors['train_accuracy'],
               graph_tensors['train']['bound'],
               graph_tensors['train']['verified'],
               graph_tensors['dual_loss_ratio'],
               graph_tensors['delta'],
               train_summ,
               graph_tensors['train_op']], input_feed)  # pylint: disable=line-too-long

      writer.add_summary(train_summ_py, step)
      if step % report_every == 0 or step == num_train_steps - 0:
        dev_total_num_correct = 0.0
        test_total_num_correct = 0.0
        dev_verified_count = 0.0
        test_verified_count = 0.0
        dev_num_batches = graph_tensors['dev_num_examples'] // batch_size
        test_num_batches = graph_tensors['test_num_examples'] // batch_size
        dev_total_num_examples = dev_num_batches * batch_size
        test_total_num_examples = test_num_batches * batch_size
        for _ in range(dev_num_batches):
          correct, verified = session.run(
              [graph_tensors['dev_num_correct'],
               graph_tensors['dev']['verified']], input_feed)
          dev_total_num_correct += correct
          dev_verified_count += np.sum(verified)
        for _ in range(test_num_batches):
          correct, verified = session.run(
              [graph_tensors['test_num_correct'],
               graph_tensors['test']['verified']], input_feed)
          test_total_num_correct += correct
          test_verified_count += np.sum(verified)
        dev_accuracy = dev_total_num_correct / dev_total_num_examples
        test_accuracy = test_total_num_correct / test_total_num_examples
        dev_verified_accuracy = dev_verified_count / dev_total_num_examples
        test_verified_accuracy = test_verified_count / test_total_num_examples
        dev_summary = tf.Summary()
        dev_summary.value.add(tag='dev_accuracy', simple_value=dev_accuracy)
        test_summary = tf.Summary()
        test_summary.value.add(tag='test_accuracy', simple_value=test_accuracy)
        writer.add_summary(dev_summary, step)
        writer.add_summary(test_summary, step)

        train_bound_summary = tf.Summary()
        train_bound_summary.value.add(tag='train_bound_summary',
                                      simple_value=np.mean(train_bound))
        writer.add_summary(train_bound_summary, step)
        train_verified_summary = tf.Summary()
        train_verified_summary.value.add(tag='train_verified_summary',
                                         simple_value=np.mean(train_verified))
        writer.add_summary(train_verified_summary, step)

        dev_verified_summary = tf.Summary()
        dev_verified_summary.value.add(tag='dev_verified_summary',
                                       simple_value=
                                       np.mean(dev_verified_accuracy))
        writer.add_summary(dev_verified_summary, step)

        test_verified_summary = tf.Summary()
        test_verified_summary.value.add(tag='test_verified_summary',
                                        simple_value=
                                        np.mean(test_verified_accuracy))
        writer.add_summary(test_verified_summary, step)

        total_loss_summary = tf.Summary()
        total_loss_summary.value.add(tag='total_loss_summary',
                                     simple_value=total_loss_np)
        writer.add_summary(total_loss_summary, step)
        dual_train_loss_summary = tf.Summary()
        dual_train_loss_summary.value.add(tag='dual_train_loss_summary',
                                          simple_value=dual_loss_np)
        writer.add_summary(dual_train_loss_summary, step)

        logging.info('dual_loss_ratio: %f, delta: %f',
                     dual_loss_ratio_val, delta_val)
        logging.info('step: %d, '
                     'train loss: %f, '
                     'dual train loss: %f, '
                     'train accuracy: %f, '
                     'dev accuracy: %f, '
                     'test accuracy: %f, ', step, loss_np,
                     dual_loss_np, train_accuracy_np,
                     dev_accuracy, test_accuracy)
        dev_verified_accuracy_mean = np.mean(dev_verified_accuracy)
        test_verified_accuracy_mean = np.mean(test_verified_accuracy)
        logging.info('Train Bound = %.05f, train verified: %.03f, '
                     'dev verified: %.03f, test verified: %.03f',
                     np.mean(train_bound),
                     np.mean(train_verified), dev_verified_accuracy_mean,
                     test_verified_accuracy_mean)
        if dev_accuracy > best_dev_accuracy:
          # Store most accurate model so far.
          network_saver.save(session.raw_session(),
                             os.path.join(snapshot_path, 'best'))
          best_dev_accuracy = dev_accuracy
          best_test_accuracy = test_accuracy
        logging.info('best dev acc\t%f\tbest test acc\t%f',
                     best_dev_accuracy, best_test_accuracy)
        if dev_verified_accuracy_mean > best_verified_dev_accuracy:
          # Store model with best verified accuracy so far.
          network_saver.save(session.raw_session(),
                             os.path.join(snapshot_path, 'best_verified'))
          best_verified_dev_accuracy = dev_verified_accuracy_mean
          best_verified_test_accuracy = test_verified_accuracy_mean
        logging.info('best verified dev acc\t%f\tbest verified test acc\t%f',
                     best_verified_dev_accuracy, best_verified_test_accuracy)

        writer.flush()

    # Store model at end of training.
    network_saver.save(session.raw_session(),
                       os.path.join(snapshot_path, 'final'))


def evaluate(config_dict, model_location):
  """Get the accuracy on the test set."""
  graph_tensor_producer = robust_model.RobustModel(**config_dict)
  graph_tensors = graph_tensor_producer()
  input_feed = {}

  network_saver = tf.train.Saver(graph_tensor_producer.variables)
  with tf.train.SingularMonitoredSession() as session:
    network_saver.restore(session.raw_session(), model_location)

    for datasplit in ['dev', 'test']:
      total_num_correct = 0.0
      total_num_batches = (graph_tensors['%s_num_examples' % datasplit]
                           // graph_tensor_producer.batch_size)
      total_num_examples = total_num_batches * graph_tensor_producer.batch_size
      logging.info('total number of examples  %d', total_num_examples)
      for _ in range(total_num_batches):
        total_num_correct += session.run(
            graph_tensors['%s_num_correct' % datasplit], input_feed)
      logging.info('%s accuracy %f', datasplit,
                   total_num_correct / total_num_examples)


def analysis(config_dict, synonym_filepath,
             model_location, batch_size, batch_offset=0,
             total_num_batches=0, datasplit='test', delta=3.0,
             num_perturbations=5, max_padded_length=0):
  """Run analysis."""
  tf.reset_default_graph()
  if datasplit not in ['train', 'dev', 'test']:
    raise ValueError('Invalid datasplit: %s' % datasplit)
  logging.info('model_location: %s', model_location)
  logging.info('num_perturbations: %d', num_perturbations)
  logging.info('delta: %f', delta)

  logging.info('Run analysis, datasplit: %s, batch %d', datasplit, batch_offset)
  synonym_keys, max_synoynm_counts, synonym_value_lens_cum, \
      synonym_values_list = construct_synonyms(synonym_filepath)

  graph_tensor_producer = robust_model.RobustModel(**config_dict)
  # Use new batch size.
  graph_tensor_producer.batch_size = batch_size
  # Overwrite the config originally in the saved snapshot.
  logging.info('old delta %f, old num_perturbations: %d',
               graph_tensor_producer.config['delta'],
               graph_tensor_producer.config['num_perturbations'])
  graph_tensor_producer.config['delta'] = delta
  graph_tensor_producer.config['num_perturbations'] = num_perturbations
  if max_padded_length > 0:
    graph_tensor_producer.config['max_padded_length'] = max_padded_length

  logging.info('new delta %f, num_perturbations: %d, max_padded_length: %d',
               graph_tensor_producer.config['delta'],
               graph_tensor_producer.config['num_perturbations'],
               graph_tensor_producer.config['max_padded_length'])
  logging.info('graph_tensors.config: %s', graph_tensor_producer.config)

  graph_tensors = graph_tensor_producer()
  network_saver = tf.train.Saver(graph_tensor_producer.variables)
  with tf.train.SingularMonitoredSession() as session:
    network_saver.restore(session.raw_session(), model_location)

    for _ in range(batch_offset):
      # Seek to the correct batch.
      session.run(graph_tensors[datasplit]['sentiment'])

    input_feed = {}
    # Tokenize synonyms
    tokenize_synonyms = [[] for _ in range(graph_tensors['vocab_size'])]
    lookup_indices_keys = session.run(graph_tensors['indices'],
                                      feed_dict={graph_tensors['lookup_token']:
                                                     synonym_keys})
    lookup_indices_values = session.run(graph_tensors['indices'],
                                        feed_dict={
                                            graph_tensors['lookup_token']:
                                            synonym_values_list})
    for i, key_index in enumerate(lookup_indices_keys):
      tokenize_synonyms[key_index] = lookup_indices_values[
          synonym_value_lens_cum[i]:synonym_value_lens_cum[i+1]].tolist()

    synonym_values_np = np.zeros([graph_tensors['vocab_size'],
                                  max_synoynm_counts])
    for i in range(graph_tensors['vocab_size']):
      # False-safe case. No perturbations. Set it as itself.
      synonym_values_np[i][0] = i
      for j in range(len(tokenize_synonyms[i])):
        synonym_values_np[i][j] = tokenize_synonyms[i][j]
    synonym_counts_np = [len(s) for s in tokenize_synonyms]
    input_feed[graph_tensors['synonym_values']] = synonym_values_np
    input_feed[graph_tensors['synonym_counts']] = synonym_counts_np

    total_num_batches = (
        graph_tensors['%s_num_examples' % datasplit] //
        batch_size) if total_num_batches == 0 else total_num_batches
    total_num_examples = total_num_batches * batch_size
    logging.info('total number of examples  %d', total_num_examples)
    logging.info('total number of batches  %d', total_num_batches)

    total_correct, total_verified = 0.0, 0.0
    for ibatch in range(total_num_batches):
      results = session.run(graph_tensors[datasplit], input_feed)
      logging.info('%s bound = %.05f,  verified: %.03f,'
                   '  nominally correct: %.03f',
                   datasplit, np.mean(results['bound']),
                   np.mean(results['verified']),
                   np.mean(results['correct']))
      total_correct += sum(results['correct'])
      total_verified += sum(results['verified'])
      for j in range(batch_size):
        logging.info({
            'datasplit': datasplit,
            'nominal': results['correct'][j],
            'verify': results['verified'][j],
            'bound': results['bound'][j],
            'i': ibatch * batch_size + j,
            'delta': delta,
            'num_perturbations': num_perturbations,
            'model_location': model_location,
            'words': results['words'][j],
            'prediction': results['predictions'][j].tolist(),
            'label': results['sentiment'][j],
        })
    total_correct /= total_num_examples
    total_verified /= total_num_examples
    logging.info('%s final correct: %.03f, verified: %.03f',
                 datasplit, total_correct, total_verified)
    logging.info({
        'datasplit': datasplit,
        'nominal': total_correct,
        'verify': total_verified,
        'delta': delta,
        'num_perturbations': num_perturbations,
        'model_location': model_location,
        'final': True
    })


def main(_):
  # Read the config file into a new ad-hoc module.
  with open(FLAGS.config_path, 'r') as config_file:
    config_code = config_file.read()
    config_module = imp.new_module('config')
    exec(config_code, config_module.__dict__)  # pylint: disable=exec-used
  config = config_module.get_config()

  config_dict = {'task': FLAGS.task,
                 'batch_size': FLAGS.batch_size,
                 'pooling': FLAGS.pooling,
                 'learning_rate': FLAGS.learning_rate,
                 'config': config,
                 'embedding_dim': config['embedding_dim'],
                 'fine_tune_embeddings': FLAGS.fine_tune_embeddings,
                 'num_oov_buckets': FLAGS.num_oov_buckets,
                 'max_grad_norm': FLAGS.max_grad_norm}

  if FLAGS.analysis:
    logging.info('Analyze model location: %s', config['model_location'])
    base_batch_offset = config['scenario_offset'] * config['scenario_size']
    analysis(config_dict, config['synonym_filepath'], config['model_location'],
             FLAGS.batch_size, base_batch_offset,
             0, datasplit=FLAGS.analysis_split,
             delta=config['delta'],
             num_perturbations=config['num_perturbations'],
             max_padded_length=config['max_padded_length'])

  elif FLAGS.evaluate:
    evaluate(config_dict, config['model_location'])

  else:
    snapshot_path = os.path.join(FLAGS.experiment_root, 'snapshot')

    if FLAGS.tb_dir is None:
      tb_dir = os.path.join(FLAGS.experiment_root, 'tensorboard')
    else:
      tb_dir = FLAGS.tb_dir

    train(config_dict, config['synonym_filepath'],
          FLAGS.batch_size,
          num_train_steps=FLAGS.num_train_steps,
          schedule_ratio=FLAGS.schedule_ratio,
          report_every=FLAGS.report_every,
          snapshot_path=snapshot_path,
          tb_dir=tb_dir)


if __name__ == '__main__':
  logging.set_stderrthreshold('info')
  app.run(main)
