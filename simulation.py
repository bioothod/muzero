from typing import Callable, Dict, List, NamedTuple

import logging

from dataclasses import dataclass
from time import perf_counter

import torch
from torch.utils.tensorboard import SummaryWriter

from hparams import GenericHparams as Hparams
from networks import GameState, Inference
import module_loader
import mcts

@dataclass
class TrainElement:
    start_index: torch.Tensor
    values: torch.Tensor
    rewards: torch.Tensor
    children_visits: torch.Tensor
    initial_game_state: torch.Tensor
    actions: torch.Tensor
    sample_len: torch.Tensor
    player_ids: torch.Tensor

    def __len__(self) -> int:
        return len(self.values)

    def __hash__(self) -> int:
        state_data = self.initial_game_state.detach().cpu().numpy().tostring()
        action_data = self.actions.detach().cpu().numpy().tostring()
        start_index_data = self.start_index.detach().cpu().numpy().tostring()
        sample_len_data = self.sample_len.detach().cpu().numpy().tostring()
        player_ids_data = self.player_ids.detach().cpu().numpy().tostring()
        return hash((state_data, action_data, start_index_data, sample_len_data, player_ids_data))

    def __eq__(self, other: 'TrainElement') -> bool:
        return torch.all(self.initial_game_state == other.initial_game_state) and torch.all(self.values == other.values) and torch.all(self.actions == other.actions)

    def to(self, device):
        self.start_index = self.start_index.to(device)
        self.values = self.values.to(device)
        self.rewards = self.rewards.to(device)
        self.children_visits = self.children_visits.to(device)
        self.initial_game_state = self.initial_game_state.to(device)
        self.actions = self.actions.to(device)
        self.sample_len = self.sample_len.to(device)
        self.player_ids = self.player_ids.to(device)
        return self

def roll_by_gather(mat, dim, shifts: torch.LongTensor):
    # assumes 2D array
    n_rows, n_cols = mat.shape

    if dim == 0:
        arange1 = torch.arange(n_rows, device=shifts.device).view((n_rows, 1)).repeat((1, n_cols))
        arange2 = (arange1 - shifts) % n_rows
        return torch.gather(mat, 0, arange2)
    elif dim == 1:
        arange1 = torch.arange(n_cols, device=shifts.device).view(( 1,n_cols)).repeat((n_rows,1))
        arange2 = (arange1 - shifts) % n_cols
        return torch.gather(mat, 1, arange2)

class GameStats:
    episode_len: torch.Tensor
    rewards: torch.Tensor
    root_values: torch.Tensor
    children_visits: torch.Tensor
    actions: torch.Tensor
    player_ids: torch.Tensor
    dones: torch.Tensor
    game_states: torch.Tensor

    def __init__(self, hparams: Hparams, logger: logging.Logger):
        self.logger = logger
        self.hparams = hparams

        self.episode_len = torch.zeros(hparams.batch_size, dtype=torch.int64, device=hparams.device)
        self.rewards = torch.zeros(hparams.batch_size, hparams.max_episode_len, dtype=torch.float32, device=hparams.device)
        self.root_values = torch.zeros(hparams.batch_size, hparams.max_episode_len, dtype=torch.float32, device=hparams.device)
        self.children_visits = torch.zeros(hparams.batch_size, hparams.num_actions, hparams.max_episode_len, dtype=torch.float32, device=hparams.device)
        self.actions = torch.zeros(hparams.batch_size, hparams.max_episode_len, dtype=torch.int64, device=hparams.device)
        self.player_ids = torch.zeros(hparams.batch_size, hparams.max_episode_len, dtype=torch.int64, device=hparams.device)
        self.dones = torch.zeros(hparams.batch_size, dtype=torch.bool, device=hparams.device)
        self.game_states = []

        self.stored_tensors = {
            'rewards': self.rewards,
            'root_values': self.root_values,
            'children_visits': self.children_visits,
            'actions': self.actions,
            'player_ids': self.player_ids,
            'dones': self.dones,
            'game_states': self.game_states,
        }

    def to(self, device):
        self.episode_len = self.episode_len.to(device)
        self.rewards = self.rewards.to(device)
        self.root_values = self.root_values.to(device)
        self.children_visits = self.children_visits.to(device)
        self.actions = self.actions.to(device)
        self.player_ids = self.player_ids.to(device)
        self.dones = self.dones.to(device)
        self.game_states = [state.to(device) for state in self.game_states]
        return self

    def __len__(self):
        return self.episode_len.sum().item()

    def append(self, index: torch.Tensor, tensors_dict: Dict[str, torch.Tensor]):
        episode_len = self.episode_len[index].long()
        index = index.long()

        for key, value in tensors_dict.items():
            if not key in self.stored_tensors:
                msg = f'invalid key: {key}, tensor shape: {value.shape}, available keys: {list(self.stored_tensors.keys())}'
                self.logger.critical(msg)
                raise ValueError(msg)

            dst = self.stored_tensors[key]
            if key == 'children_visits':
                dst[index, :, episode_len] = value.detach().clone()
                new_visits = dst[index, :, episode_len]

                if torch.any(value != new_visits):
                    raise ValueError(f'could not update tensor in place:\nvalue:\n{value[:10]}\nnew_visits:\n{new_visits}')
            elif key == 'dones':
                dst[index] = value.detach().clone()
            elif key == 'game_states':
                dst.append(value.detach().clone())
            else:
                dst[index, episode_len] = value.detach().clone()

        self.episode_len[index] += 1

    def update_last_reward_and_values(self, win_index: torch.Tensor, rewards: torch.Tensor):
        episode_len = self.episode_len[win_index].long()
        self.rewards[win_index, episode_len-1] = rewards

    def make_target(self, start_index: torch.Tensor) -> List[TrainElement]:
        target_values = torch.zeros(len(start_index), self.hparams.num_unroll_steps+1, dtype=self.hparams.dtype, device=start_index.device)
        target_rewards = torch.zeros(len(start_index), self.hparams.num_unroll_steps+1, dtype=self.hparams.dtype, device=start_index.device)
        target_children_visits = torch.zeros(len(start_index), self.hparams.num_actions, self.hparams.num_unroll_steps+1, dtype=self.hparams.dtype, device=start_index.device)
        taken_actions = torch.zeros(len(start_index), self.hparams.num_unroll_steps+1, dtype=torch.int64, device=start_index.device)
        player_ids = torch.zeros(len(start_index), self.hparams.num_unroll_steps+1, dtype=torch.int64, device=start_index.device)

        sample_len = torch.zeros(len(start_index), dtype=torch.int64, device=start_index.device)

        batch_index = torch.arange(len(start_index), dtype=torch.int64, device=start_index.device)
        initial_game_state = []
        for bidx, sidx in zip(batch_index, start_index):
            initial_game_state.append(self.game_states[sidx][bidx, :, :, :])

        if start_index.device != self.episode_len.device:
            msg = f'start_index: {start_index.device}, self.episode_len: {self.episode_len.device}'
            self.logger.critical(msg)
            raise ValueError(msg)

        discount_mult = torch.logspace(0, 1, self.hparams.td_steps, base=self.hparams.value_discount).to(start_index.device)
        discount_mult = discount_mult.unsqueeze(0).tile([len(start_index), 1])
        all_rewards_index = torch.arange(0, self.rewards.shape[1]).unsqueeze(0).tile([len(start_index), 1]).to(start_index.device)

        for unroll_step in range(0, self.hparams.num_unroll_steps+1):
            start_unroll_index = start_index + unroll_step
            bootstrap_index = start_unroll_index + self.hparams.td_steps
            bootstrap_update_index = bootstrap_index < self.episode_len

            # self.logger.info(f'{unroll_index}: '
            #              f'start_index: {start_index.cpu().numpy()}, '
            #              f'current_index: {current_index.cpu().numpy()}, '
            #              f'bootstrap_index: {bootstrap_index.cpu().numpy()}, '
            #              f'bootstrap_update_index: {bootstrap_update_index.cpu().numpy()}/{bootstrap_update_index.shape}, '
            #              )
            values = torch.zeros(len(start_index), device=start_index.device).float()
            if bootstrap_update_index.sum() > 0:
                #self.logger.info(f'bootstrap_update_index: {bootstrap_update_index}')
                valid_batch_index = batch_index[bootstrap_update_index]
                last_discount = self.hparams.value_discount ** self.hparams.td_steps
                values[bootstrap_update_index] = self.root_values[valid_batch_index, bootstrap_index].float() * last_discount

            start_unroll_valid_bool_index = start_unroll_index < self.episode_len
            #start_unroll_valid_index = start_unroll_index[start_unroll_valid_bool_index]

            rewards = torch.where(all_rewards_index < start_unroll_index.unsqueeze(1), 0, self.rewards)
            rewards = torch.where(all_rewards_index >= bootstrap_index.unsqueeze(1), 0, rewards)

            discount = roll_by_gather(discount_mult, 1, start_unroll_index.unsqueeze(1))
            discounted_rewards = rewards * discount
            discounted_rewards = discounted_rewards.sum(1)
            values += discounted_rewards

            #target_values[:, unroll_step] = (values + self.root_values[:, unroll_step]) / 2
            target_values[:, unroll_step] = values
            target_children_visits[:, :, unroll_step] = self.children_visits[:, :, unroll_step].float()
            player_ids[:, unroll_step] = self.player_ids[:, unroll_step].long()

            target_rewards[:, unroll_step] = self.rewards[:, unroll_step].float()
            taken_actions[:, unroll_step] = self.actions[:, unroll_step].long()

            sample_len[start_unroll_valid_bool_index] += 1

        samples = []
        for i in range(len(target_values)):
            elm = TrainElement(
                start_index=start_index[i],
                values=target_values[i],
                rewards=target_rewards[i],
                children_visits=target_children_visits[i],
                initial_game_state=initial_game_state[i],
                actions=taken_actions[i],
                sample_len=sample_len[i],
                player_ids=player_ids[i],
            )
            samples.append(elm)

        return samples


class Train:
    def __init__(self,
                 game_ctl: module_loader.GameModule,
                 inference: Inference,
                 logger: logging.Logger,
                 summary_writer: SummaryWriter,
                 summary_prefix: str,
                 action_selection_fn: Callable):
        self.game_ctl = game_ctl
        self.hparams = game_ctl.hparams
        self.logger = logger
        self.inference = inference
        self.summary_writer = summary_writer
        self.summary_prefix = summary_prefix
        self.summary_step = 0

        self.action_selection_fn = action_selection_fn

        self.game_stats = {player_id:GameStats(game_ctl.hparams, self.logger) for player_id in self.hparams.player_ids}

    def run_simulations(self, initial_player_id: torch.Tensor, initial_game_state: torch.Tensor, invalid_actions_mask: torch.Tensor):
        start_simulation_time = perf_counter()

        tree = mcts.Tree(self.hparams, initial_player_id, self.inference, self.logger)

        batch_size = len(initial_player_id)
        batch_index = torch.arange(batch_size).long().to(self.hparams.device)
        node_index = torch.zeros(batch_size, 1).long().to(self.hparams.device)

        out = self.inference.initial(initial_game_state)

        episode_len = torch.ones(batch_size, dtype=torch.int64).to(self.hparams.device)
        search_path = torch.zeros(batch_size, 1, dtype=torch.int64).to(self.hparams.device)

        tree.store_states(search_path, episode_len, out.hidden_state)

        tree.expand(node_index, out.policy_logits)
        tree.visit_count.scatter_(1, node_index, 1)
        tree.value_sum.scatter_(1, node_index, out.value)

        if self.hparams.add_exploration_noise:
            children_index = tree.children_index(batch_index, node_index)
            tree.add_exploration_noise(children_index, self.hparams.exploration_fraction)

        for _ in range(self.hparams.num_simulations):
            search_path, episode_len = tree.run_one_simulation(initial_player_id.detach().clone(), invalid_actions_mask.detach().clone())

        simulation_time = perf_counter() - start_simulation_time
        one_sim_ms = int(simulation_time / self.hparams.num_simulations * 1000)

        children_index = tree.children_index(batch_index, node_index)
        children_visit_counts = tree.visit_count.gather(1, children_index).float()
        root_values = tree.value(batch_index, node_index).squeeze(1)

        actions = self.action_selection_fn(children_visit_counts, episode_len)
        # max_debug = 10
        # self.logger.info(f'children_index:\n{children_index[:max_debug]}\n'
        #                  f'children_visit_counts:\n{children_visit_counts[:max_debug]}\n'
        #                  f'children_sum_visits:\n{children_sum_visits[:max_debug]}\n'
        #                  f'children_visits:\n{children_visits[:max_debug]}\n'
        #                  f'root_values: {root_values.shape}\n{root_values[:max_debug]}\n'
        #                  f'actions:\n{actions[:max_debug]}')
        return actions, children_visit_counts, root_values

def run_single_game(hparams: Hparams, train: Train, num_steps: int) -> Dict[int, GameStats]:
    game_states = torch.zeros(hparams.batch_size, *hparams.state_shape, dtype=torch.float32, device=hparams.device)
    player_ids = torch.ones(hparams.batch_size, device=hparams.device, dtype=torch.int64) * hparams.player_ids[0]

    active_games_index = torch.arange(hparams.batch_size).long().to(hparams.device)
    game_state_stacks = {player_id:GameState(hparams.batch_size, hparams, train.game_ctl.network_hparams) for player_id in hparams.player_ids}

    while True:
        active_player_ids = player_ids[active_games_index].detach().clone()

        # we do not care if it will be modified in place, we will make a copy when pushing this state into the stack of states
        active_game_states = game_states[active_games_index]
        invalid_actions_mask = train.game_ctl.invalid_actions_mask(train.game_ctl.game_hparams, active_game_states)

        player_id = active_player_ids[0].item()
        if torch.any(player_id != player_ids):
            raise ValueError(f'pushing non-consistent player_ids: player_id: {player_id}, not_equal: {(player_id != player_ids).sum()}/{len(player_ids)}')

        game_state_stacks[player_id].push_game(player_ids, game_states)
        game_state_stack_converted = game_state_stacks[player_id].create_state()

        actions, children_visits, root_values = train.run_simulations(active_player_ids, game_state_stack_converted[active_games_index], invalid_actions_mask)
        new_game_states, rewards, dones = train.game_ctl.step_games(train.game_ctl.game_hparams, active_game_states, active_player_ids, actions)
        game_states[active_games_index] = new_game_states.detach().clone()

        train.game_stats[player_id].append(active_games_index, {
            'children_visits': children_visits,
            'root_values': root_values,
            'game_states': game_state_stack_converted, # needs to save whole tensor of batch_size size
            'rewards': rewards,
            'actions': actions,
            'dones': dones,
            'player_ids': active_player_ids,
        })

        win_index = active_games_index[torch.logical_and((dones == True), (rewards > 0))]
        other_player_id = mcts.player_id_change(hparams, torch.tensor(player_id)).item()
        other_rewards = torch.ones_like(win_index).float() * -1
        train.game_stats[other_player_id].update_last_reward_and_values(win_index, other_rewards)

        # max_debug = 10
        # train.logger.info(f'game:\n{game_states[0].detach().cpu().numpy().astype(int)}\n'
        #                   f'actions:\n{actions[:max_debug]}\n'
        #                   f'children_visits:\n{children_visits[:max_debug]}\n'
        #                   f'root_values:\n{root_values[:max_debug]}\n'
        #                   f'rewards:\n{rewards[:max_debug]}\n'
        #                   f'dones:\n{dones[:max_debug]}\n'
        #                   f'player_ids:\n{player_ids[:max_debug]}\n'
        #                   f'active_game_index:\n{active_games_index[:max_debug]}'
        #                   )

        if dones.sum() == len(dones):
            break

        player_ids = mcts.player_id_change(hparams, player_ids)
        active_games_index = active_games_index[dones != True]

        num_steps -= 1
        if num_steps == 0:
            break

    return train.game_stats
