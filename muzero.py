from typing import Dict, List, Optional

import argparse
import dataclasses
import itertools
import logging
import pickle
import os
import random
import time

from collections import defaultdict
from copy import deepcopy
from time import perf_counter

import numpy as np
from replay_buffer import ReplayBuffer
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter

torch.backends.cuda.matmul.allow_tf32 = True

from evaluate_score import EvaluationDataset
from hparams import GenericHparams as Hparams
from logger import setup_logger
import module_loader
import muzero_server
import networks
import simulation

def scale_gradient(tensor: torch.Tensor, scale: torch.Tensor) -> torch.Tensor:
    return tensor * scale + tensor.detach() * (1 - scale)
    #return tensor

def train_element_collate_fn(samples: List[simulation.TrainElement]):
    collated_dict = defaultdict(list)
    for sample in samples:
        sample = dataclasses.asdict(sample)
        for key, value in sample.items():
            collated_dict[key].append(value)

    converted_dict = {}
    for key, list_value in collated_dict.items():
        converted_dict[key] = torch.stack(list_value, 0)

    return simulation.TrainElement(**converted_dict)

def action_selection_fn(children_visit_counts: torch.Tensor, episode_len: torch.Tensor):
    actions = torch.argmax(children_visit_counts, 1)
    return actions

class Trainer:
    def __init__(self, game_ctl: module_loader.GameModule, logger: logging.Logger, eval_ds: Optional[EvaluationDataset]):
        self.game_ctl = game_ctl
        self.hparams = game_ctl.hparams
        self.logger = logger
        self.eval_ds = eval_ds

        self.max_best_score = 0.
        self.max_good_score = 0.

        self.replay_buffer = ReplayBuffer(self.hparams)

        self.grpc_server, self.muzero_server = muzero_server.start_server(self.hparams, self.replay_buffer, logger)

        tensorboard_log_dir = os.path.join(self.hparams.checkpoints_dir, 'tensorboard_logs')
        first_run = True
        if os.path.exists(tensorboard_log_dir) and len(os.listdir(tensorboard_log_dir)) > 0:
            first_run = False

        self.summary_writer = SummaryWriter(log_dir=tensorboard_log_dir)
        self.global_step = 0

        self.inference = networks.Inference(self.game_ctl, logger)

        self.representation_opt = torch.optim.Adam(self.inference.representation.parameters(), lr=self.hparams.init_lr)
        self.prediction_opt = torch.optim.Adam(self.inference.prediction.parameters(), lr=self.hparams.init_lr)
        self.dynamic_opt = torch.optim.Adam(self.inference.dynamic.parameters(), lr=self.hparams.init_lr)
        self.optimizers = [self.representation_opt, self.prediction_opt, self.dynamic_opt]

        self.ce_loss = nn.CrossEntropyLoss(reduction='none')
        self.scalar_loss = nn.MSELoss(reduction='none')

        self.all_games: Dict[int, List[simulation.GameStats]] = defaultdict(list)

        self.try_load()
        self.save_muzero_server_weights()

    def save_muzero_server_weights(self):
        save_dict = {
            'representation_state_dict': self.inference.representation.state_dict(),
            'prediction_state_dict': self.inference.prediction.state_dict(),
            'dynamic_state_dict': self.inference.dynamic.state_dict(),
        }
        meta = pickle.dumps(save_dict)

        self.muzero_server.update_weights(self.global_step, meta)

    def close(self):
        self.grpc_server.wait_for_termination()

    def policy_loss(self, policy_logits: torch.Tensor, children_visit_counts_for_step: torch.Tensor) -> torch.Tensor:
        # children_visit_counts: [B, Nactions]
        children_visits_sum = children_visit_counts_for_step.sum(1, keepdim=True)
        action_probs = children_visit_counts_for_step / children_visits_sum
        loss = self.ce_loss(policy_logits, action_probs)
        return loss

    def training_step(self, sample: simulation.TrainElement):
        sample_len_max = sample.sample_len.max().item()
        sample.sample_len = sample.sample_len.float()

        out = self.inference.initial(sample.player_ids[:, 0], sample.game_states)

        policy_loss = self.policy_loss(out.policy_logits, sample.children_visits[:, :, 0])
        value_loss = self.scalar_loss(out.value, sample.values[:, 0])

        iteration_loss = policy_loss + value_loss
        total_loss_mean = torch.mean(iteration_loss)

        policy_loss_mean = policy_loss.mean()
        value_loss_mean = value_loss.mean()
        reward_loss_mean = 0

        self.summary_writer.add_scalars('train/initial_losses', {
                'policy': policy_loss_mean,
                'value': value_loss_mean,
                'total': total_loss_mean,
        }, self.global_step)

        for player_id in self.hparams.player_ids:
            player_idx = sample.player_ids[:, 0] == player_id

            if player_idx.sum() > 0:
                pred_values = out.value[player_idx].detach().cpu().numpy()
                true_values = sample.values[player_idx, 0].detach().cpu().numpy()
                value_loss_local = value_loss[player_idx]

                self.summary_writer.add_scalars(f'train/initial_values{player_id}', {
                    f'pred': pred_values.mean(),
                    f'true': true_values.mean(),
                    f'loss': value_loss_local.mean(),
                }, self.global_step)

        batch_index = torch.arange(len(sample))
        for step_idx in range(1, sample_len_max):
            len_idx = step_idx < sample.sample_len[batch_index]
            batch_index = batch_index[len_idx]
            sample_len = sample.sample_len[batch_index]
            actions = sample.actions[batch_index]
            values = sample.values[batch_index]
            children_visits = sample.children_visits[batch_index]
            rewards = sample.rewards[batch_index]

            hidden_states = out.hidden_state[len_idx]

            out = self.inference.recurrent(hidden_states, actions[:, step_idx-1])

            # scale = torch.ones_like(hidden_states, device=out.hidden_state.device) * 0.5
            # hidden_states = scale_gradient(hidden_states, scale)

            policy_loss = self.policy_loss(out.policy_logits, children_visits[:, :, step_idx])
            value_loss = self.scalar_loss(out.value, values[:, step_idx])
            reward_loss = self.scalar_loss(out.reward, rewards[:, step_idx-1])

            iteration_loss = policy_loss + value_loss + reward_loss
            iteration_loss = scale_gradient(iteration_loss, 1/sample_len)

            total_loss_mean += torch.mean(iteration_loss)
            policy_loss_mean += policy_loss.mean()
            value_loss_mean += value_loss.mean()
            reward_loss_mean += reward_loss.mean()

        self.summary_writer.add_scalars('train/final_losses', {
                'policy': policy_loss_mean,
                'reward': reward_loss_mean,
                'value': value_loss_mean,
                'total': total_loss_mean,
        }, self.global_step)

        return total_loss_mean

    def run_training(self):
        while self.replay_buffer.num_games() == 0:
            time.sleep(1)

        self.inference.train(True)

        for _ in range(self.hparams.num_training_steps):
            sample = self.replay_buffer.sample(batch_size=self.hparams.batch_size)[:self.hparams.batch_size]
            random.shuffle(sample)
            sample = train_element_collate_fn(sample)
            sample = sample.to(self.hparams.device)

            # do not need to call optimizers zero_grad() because we are settig grads to zero in every model
            self.inference.zero_grad()

            total_loss = self.training_step(sample)

            total_loss.backward()

            for opt in self.optimizers:
                opt.step()

            self.summary_writer.add_scalar('train/total_loss', total_loss, self.global_step)

            self.global_step += 1
            self.save_muzero_server_weights()

        self.run_evaluation(save_if_best=True)

    def run_evaluation(self, save_if_best: bool):
        if self.eval_ds is None:
            return

        start_time = perf_counter()
        hparams = deepcopy(self.hparams)
        hparams.batch_size = len(self.eval_ds.game_states)

        train = simulation.Train(self.game_ctl, self.inference, self.logger, self.summary_writer, 'eval', action_selection_fn)
        with torch.no_grad():
            active_game_states = self.eval_ds.game_states
            active_player_ids = self.eval_ds.game_player_ids
            pred_actions, children_visits, root_values = train.run_simulations(active_player_ids, active_game_states)

        best_score, good_score, total_best_score, total_good_score = self.eval_ds.evaluate(pred_actions)

        eval_time = perf_counter() - start_time
        for player_id in self.hparams.player_ids:
            self.summary_writer.add_scalars(f'eval/ref_moves_score{player_id}', {
                'good': good_score[player_id],
                'best': best_score[player_id],
            }, self.global_step)

        self.summary_writer.add_scalar('eval/time', eval_time, self.global_step)
        self.summary_writer.add_scalars(f'eval/ref_moves_score_total', {
            'max_good': self.max_good_score,
            'good': total_good_score,
            'max_best': self.max_best_score,
            'best': total_best_score,
        }, self.global_step)

        if save_if_best and (total_best_score >= self.max_best_score):
            self.max_best_score = total_best_score
            checkpoint_path = os.path.join(self.hparams.checkpoints_dir, f'muzero_best_{total_best_score:.1f}.ckpt')
            self.save(checkpoint_path)
            self.logger.info(f'stored checkpoint: generation: {self.global_step}, best_score: {total_best_score:.1f}, checkpoint: {checkpoint_path}')

        if save_if_best and (total_good_score >= self.max_good_score):
            self.max_good_score = total_good_score
            checkpoint_path = os.path.join(self.hparams.checkpoints_dir, f'muzero_good_{total_good_score:.1f}.ckpt')
            self.save(checkpoint_path)
            self.logger.info(f'stored checkpoint: generation: {self.global_step}, good_score: {total_good_score:.1f}, checkpoint: {checkpoint_path}')

    def save(self, checkpoint_path):
        torch.save({
            'representation_state_dict': self.inference.representation.state_dict(),
            'representation_optimizer_state_dict': self.representation_opt.state_dict(),
            'prediction_state_dict': self.inference.prediction.state_dict(),
            'prediction_optimizer_state_dict': self.prediction_opt.state_dict(),
            'dynamic_state_dict': self.inference.dynamic.state_dict(),
            'dynamic_optimizer_state_dict': self.dynamic_opt.state_dict(),
            'global_step': self.global_step,
            'max_best_score': self.max_best_score,
            'max_good_score': self.max_good_score,
        }, checkpoint_path)

    def load(self, checkpoint_path):
        checkpoint = torch.load(checkpoint_path)

        self.inference.representation.load_state_dict(checkpoint['representation_state_dict'])
        self.representation_opt.load_state_dict(checkpoint['representation_optimizer_state_dict'])
        self.inference.prediction.load_state_dict(checkpoint['prediction_state_dict'])
        self.prediction_opt.load_state_dict(checkpoint['prediction_optimizer_state_dict'])
        self.inference.dynamic.load_state_dict(checkpoint['dynamic_state_dict'])
        self.dynamic_opt.load_state_dict(checkpoint['dynamic_optimizer_state_dict'])

        self.global_step = int(checkpoint['global_step'])
        self.max_best_score = float(checkpoint['max_best_score'])
        self.max_good_score = float(checkpoint['max_good_score'])

        self.save_muzero_server_weights()

        self.logger.info(f'loaded checkpoint {checkpoint_path}')

    def try_load(self):
        max_score = None
        max_score_fn = None
        for fn in os.listdir(self.hparams.checkpoints_dir):
            if not fn.endswith('.ckpt'):
                continue
            if not fn.startswith('muzero_best_'):
                continue

            filename = os.path.splitext(fn)[0]
            score_str = filename.split('_')[-1]
            score = float(score_str)
            if max_score is None or score > max_score:
                max_score = score
                max_score_fn = fn

        if max_score_fn is not None:
            checkpoint_path = os.path.join(self.hparams.checkpoints_dir, max_score_fn)
            self.load(checkpoint_path)


def main():
    #torch.autograd.set_detect_anomaly(True)

    parser = argparse.ArgumentParser()
    parser.add_argument('--num_eval_simulations', type=int, default=400, help='Number of evaluation simulations')
    parser.add_argument('--num_training_steps', type=int, default=40, help='Number of training steps before evaluation')
    parser.add_argument('--checkpoints_dir', type=str, required=True, help='Checkpoints directory')
    parser.add_argument('--game', type=str, required=True, help='Name of the game')
    FLAGS = parser.parse_args()

    #os.environ['CUDA_LAUNCH_BLOCKING'] = '1'

    module = module_loader.GameModule(FLAGS.game, load=True)

    module.hparams.num_simulations = FLAGS.num_eval_simulations
    module.hparams.num_training_steps = FLAGS.num_training_steps
    module.hparams.checkpoints_dir = FLAGS.checkpoints_dir
    module.hparams.device = torch.device('cuda:0')

    logfile = os.path.join(module.hparams.checkpoints_dir, 'muzero.log')
    os.makedirs(module.hparams.checkpoints_dir, exist_ok=True)
    logger = setup_logger('muzero', logfile, module.hparams.log_to_stdout)

    refmoves_fn = 'refmoves1k_kaggle'
    if FLAGS.game == 'connectx':
        eval_ds = EvaluationDataset(refmoves_fn, module.hparams, logger)
    else:
        eval_ds = None

    trainer = Trainer(module, logger, eval_ds)

    while True:
        trainer.run_training()

if __name__ == '__main__':
    main()
