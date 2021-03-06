from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import time
import random
from threading import Thread

from six.moves import range  # pylint: disable=redefined-builtin
import numpy as np
import tensorflow as tf

from reinforceflow.core.base_agent import BaseDQNAgent
from reinforceflow.core import EGreedyPolicy
from reinforceflow import misc
from reinforceflow import logger
from reinforceflow.misc import discount_rewards


class AsyncDQNAgent(BaseDQNAgent):
    """Constructs Asynchronous N-step Q-Learning agent, based on paper:
    "Asynchronous Methods for Deep Reinforcement Learning", Mnih et al., 2015.
    (https://arxiv.org/abs/1602.01783v2)

    See `core.base_agent.BaseDQNAgent.__init__`.
    """
    def __init__(self, env, net_fn, use_gpu=False, name='AsyncDQN'):
        super(AsyncDQNAgent, self).__init__(env=env, net_fn=net_fn, name=name)
        config = tf.ConfigProto(
            device_count={'GPU': use_gpu}
        )
        config.gpu_options.allow_growth = True
        self.sess = tf.Session(config=config)
        self._build_inference_graph(self.env)
        self.weights = self._weights
        self.request_stop = False
        self._prev_obs_step = None
        self._prev_opt_step = None
        self._last_time = None
        self.writer = None
        self.sess.run(tf.global_variables_initializer())

    def _write_summary(self, test_episodes=3):
        test_r, test_q = self.test(episodes=test_episodes)
        obs_step = self.obs_counter
        obs_per_sec = (self.obs_counter - self._prev_obs_step) / (time.time() - self._last_time)
        opt_per_sec = (self.step_counter - self._prev_opt_step) / (time.time() - self._last_time)
        self._last_time = time.time()
        self._prev_obs_step = obs_step
        self._prev_opt_step = self.step_counter
        logger.info("Global agent greedy eval: Average R: %.2f. Average maxQ: %.2f. Step: %d."
                    % (test_r, test_q, obs_step))
        logger.info("Performance. Observation/sec: %0.2f. Update/sec: %0.2f."
                    % (obs_per_sec, opt_per_sec))
        logs = [tf.Summary.Value(tag=self._scope + 'greedy_r', simple_value=test_r),
                tf.Summary.Value(tag=self._scope + 'greedy_q', simple_value=test_q),
                tf.Summary.Value(tag='performance/observation/sec', simple_value=obs_per_sec),
                tf.Summary.Value(tag='performance/update/sec', simple_value=opt_per_sec)
                ]
        self.writer.add_summary(tf.Summary(value=logs), global_step=obs_step)

    def train(self,
              num_threads,
              steps,
              optimizer,
              learning_rate,
              log_dir,
              epsilon_steps,
              target_freq,
              log_freq,
              optimizer_args=None,
              gradient_clip=40.0,
              decay=None,
              decay_args=None,
              epsilon_pool=(0.1, 0.01, 0.5),
              gamma=0.99,
              batch_size=32,
              render=False,
              saver_keep=10):
        if num_threads < 1:
            raise ValueError("Number of threads must be >= 1 (Got: %s)." % num_threads)
        thread_agents = []
        envs = []
        if not isinstance(epsilon_pool, (list, tuple, np.ndarray)):
            epsilon_pool = list(epsilon_pool)
        self._build_train_graph(optimizer, learning_rate, optimizer_args=optimizer_args,
                                decay=decay, decay_args=decay_args,
                                gradient_clip=gradient_clip, saver_keep=saver_keep)
        for t in range(num_threads):
            eps_min = random.choice(epsilon_pool)
            logger.debug("Sampling minimum epsilon = %0.2f for Thread-Learner #%d." % (eps_min, t))
            policy = EGreedyPolicy(eps_start=1.0, eps_final=eps_min, anneal_steps=epsilon_steps)
            env = self.env.copy()
            envs.append(env)
            agent = _ThreadDQNLearner(env=env,
                                      net_fn=self.net_fn,
                                      global_agent=self,
                                      steps=steps,
                                      optimizer=optimizer,
                                      learning_rate=learning_rate,
                                      target_freq=target_freq,
                                      policy=policy,
                                      log_freq=log_freq,
                                      optimizer_args=optimizer_args,
                                      decay=decay,
                                      decay_args=decay_args,
                                      gradient_clip=gradient_clip,
                                      gamma=gamma,
                                      batch_size=batch_size,
                                      saver_keep=saver_keep,
                                      name='ThreadLearner%d' % t)
            thread_agents.append(agent)
        self.writer = tf.summary.FileWriter(log_dir, self.sess.graph)
        self.sess.run(tf.global_variables_initializer())
        if log_dir and tf.train.latest_checkpoint(log_dir) is not None:
            self.load_weights(log_dir)
        last_log_step = self.obs_counter
        last_target_update = last_log_step

        for t in thread_agents:
            t.daemon = True
            t.start()
        self.request_stop = False

        def has_live_threads():
            return True in [th.isAlive() for th in thread_agents]

        self._prev_obs_step = self.obs_counter
        self._prev_opt_step = self.step_counter
        self._last_time = time.time()
        while has_live_threads() and self.obs_counter < steps:
            try:
                if render:
                    for env in envs:
                        env.render()
                    time.sleep(0.01)
                step = self.obs_counter
                if step - last_log_step >= log_freq:
                    last_log_step = step
                    self._write_summary()
                    self.save_weights(log_dir)
                if step - last_target_update >= target_freq:
                    last_target_update = step
                    self.target_update()
            except KeyboardInterrupt:
                logger.info('Caught Ctrl+C! Stopping training process.')
                self.request_stop = True
        self.save_weights(log_dir)
        logger.info('Training finished!')
        self.writer.close()
        for agent in thread_agents:
            agent.close()

    def train_on_batch(self, obs, actions, rewards, summarize=False):
        raise NotImplementedError('Training on batch is not supported. Use `train` method instead.')


class _ThreadDQNLearner(BaseDQNAgent, Thread):
    def __init__(self,
                 env,
                 net_fn,
                 global_agent,
                 steps,
                 optimizer,
                 learning_rate,
                 target_freq,
                 policy,
                 log_freq,
                 optimizer_args=None,
                 decay=None,
                 decay_args=None,
                 gradient_clip=40.0,
                 gamma=0.99,
                 batch_size=32,
                 saver_keep=10,
                 name=''):
        super(_ThreadDQNLearner, self).__init__(env=env, net_fn=net_fn, name=name)
        self.global_agent = global_agent
        self.sess = global_agent.sess
        self._build_inference_graph(self.env)
        self._build_train_graph(optimizer, learning_rate, optimizer_args, decay, decay_args,
                                gradient_clip, saver_keep)
        self.steps = steps
        self.target_freq = target_freq
        self.policy = policy
        self.log_freq = log_freq
        self.gamma = gamma
        self.batch_size = batch_size

    def _build_train_graph(self, optimizer, learning_rate, optimizer_args=None,
                           decay=None, decay_args=None, gradient_clip=40.0, saver_keep=10):
        # TODO: fix Variable already exists bug when creating the 2nd agent in the same scope
        with tf.variable_scope(self._scope + 'optimizer'):
            self.opt, self._lr = misc.create_optimizer(optimizer, learning_rate,
                                                       optimizer_args=optimizer_args,
                                                       decay=decay, decay_args=decay_args,
                                                       global_step=self.global_step)
            self._action_one_hot = tf.one_hot(self._action, self.env.action_shape, 1.0, 0.0,
                                              name='action_one_hot')
            q_value = tf.reduce_sum(tf.multiply(self._q, self._action_one_hot), axis=1)
            self._loss = tf.reduce_mean(tf.square(self._reward - q_value), name='loss')
            self._grads = tf.gradients(self._loss, self._weights)
            if gradient_clip:
                self._grads, _ = tf.clip_by_global_norm(self._grads, gradient_clip)
            self._grads_vars = list(zip(self._grads, self.global_agent.weights))
            self._train_op = self.global_agent.opt.apply_gradients(self._grads_vars,
                                                                   self.global_agent.global_step)
            self._sync_op = [self._weights[i].assign(self.global_agent.weights[i])
                             for i in range(len(self._weights))]
        for grad, w in self._grads_vars:
            tf.summary.histogram(w.name, w)
            tf.summary.histogram(w.name + '/gradients', grad)
        with tf.variable_scope(self._scope):
            if len(self.env.observation_shape) == 1:
                tf.summary.histogram('observation', self._obs)
            elif len(self.env.observation_shape) <= 3:
                tf.summary.image('observation', self._obs)
            else:
                logger.warn('Cannot create summary for observation with shape %s'
                            % self.env.obs_shape)
            tf.summary.histogram('action', self._action_one_hot)
            tf.summary.histogram('reward_per_action', self._q)
            tf.summary.scalar('loss', self._loss)
            self._summary_op = tf.summary.merge(tf.get_collection(tf.GraphKeys.SUMMARIES,
                                                                  self._scope))

    def _sync_global(self):
        if self._sync_op is not None:
            self.sess.run(self._sync_op)

    def run(self):
        ep_reward = misc.IncrementalAverage()
        ep_q = misc.IncrementalAverage()
        reward_accum = 0
        prev_step = self.global_agent.obs_counter
        obs = self.env.reset()
        term = True
        while not self.global_agent.request_stop:
            self._sync_global()
            batch_obs, batch_rewards, batch_actions = [], [], []
            if term:
                term = False
                obs = self.env.reset()
            while not term and len(batch_obs) < self.batch_size:
                current_step = self.global_agent.increment_obs_counter()
                reward_per_action = self.predict(obs)
                batch_obs.append(obs)
                action = self.policy.select_action(self.env, reward_per_action, current_step)
                obs, reward, term, info = self.env.step(action)
                reward_accum += reward
                reward = np.clip(reward, -1, 1)
                batch_rewards.append(reward)
                batch_actions.append(action)
            expected_reward = 0
            if not term:
                # TODO: Clip expected reward?
                expected_reward = np.max(self.global_agent.target_predict(obs))
                ep_q.add(expected_reward)
            else:
                ep_reward.add(reward_accum)
                reward_accum = 0
            batch_rewards = discount_rewards(batch_rewards, self.gamma, expected_reward)
            summarize = (term
                         and self.log_freq
                         and self.global_agent.obs_counter - prev_step > self.log_freq)
            summary_str = self._train_on_batch(np.vstack(batch_obs), batch_actions,
                                               batch_rewards, summarize)
            if summarize:
                prev_step = self.global_agent.obs_counter
                train_r = ep_reward.reset()
                train_q = ep_q.reset()
                logger.info("%s on-policy eval: Average R: %.2f. Average maxQ: %.2f. Step: %d. "
                            % (self._scope, train_r, train_q, prev_step))
                if summary_str:
                    logs = [tf.Summary.Value(tag=self._scope + 'train_r', simple_value=train_r),
                            tf.Summary.Value(tag=self._scope + 'train_q', simple_value=train_q),
                            tf.Summary.Value(tag=self._scope + 'epsilon',
                                             simple_value=self.policy.epsilon)
                            ]
                    self.global_agent.writer.add_summary(tf.Summary(value=logs),
                                                         global_step=prev_step)
                    self.global_agent.writer.add_summary(summary_str, global_step=prev_step)

    def close(self):
        pass

    def train_on_batch(self, *args, **kwargs):
        raise NotImplementedError('Use `AsyncDQNAgent.train`.')

    def train(self, *args, **kwargs):
        raise NotImplementedError('Use `AsyncDQNAgent.train`.')
