import os
import argparse
import torch
import numpy as np
from custom_envs.mpe import simple_spread_c_v2
from pathlib import Path
from gymnasium.spaces.utils import flatdim
from matplotlib import pyplot as plt
from algorithms.resource_aware_maddpg import RA_MADDPG
from utils.buffer import ReplayBuffer
from torch.utils.tensorboard import SummaryWriter

USE_CUDA = torch.cuda.is_available()
device = torch.device("cuda" if USE_CUDA else "cpu")
update_counter = 0

def create_run_dir(experiment_name):
  run_dir = Path(os.path.split(os.path.dirname(os.path.abspath(__file__)))[
                   0] + "/runs") / "maddpg" / experiment_name
  if not run_dir.exists():
    os.makedirs(str(run_dir))
    curr_run = 'run1'
  exst_run_nums = [int(str(folder.name).split('run')[1]) for folder in run_dir.iterdir() if str(folder.name).startswith('run')]
  if len(exst_run_nums) == 0:
    curr_run = 'run1'
  else:
    curr_run = 'run%i' % (max(exst_run_nums) + 1)

  run_dir = run_dir / curr_run
  if not run_dir.exists():
      os.makedirs(str(run_dir))
      os.makedirs(str(run_dir) + '/models')

  return str(run_dir)

def dict_to_tensor(d, unsqueeze_axis=0):
  d = list(d.values())
  d = np.array(d)
  d = torch.tensor(d).unsqueeze(unsqueeze_axis)
  return d

def preprocess_obs(obs):
  obs = dict_to_tensor(obs)
  return obs


def get_actions(obs, env, agents, training=True):
  actions = {}
  logits = agents.step(obs, training)
  for i, agent in enumerate(env.possible_agents):
    actions[agent] = logits[i]

  return actions, logits


def run_episode(env, agents, replay_buffer, args, training=True):
    global update_counter, writer

    obs = env.reset()
    obs = preprocess_obs(obs)

    tot_reward = np.zeros(agents.n_agents)
    steps = 0
    tot_comms = 0

    while env.agents:
      actions, logits = get_actions(obs.to(device), env, agents,
                                    training=training)
      next_obs, rewards, dones, _, infos = env.step(actions)

      next_obs = preprocess_obs(next_obs)
      rewards = dict_to_tensor(rewards)
      dones = dict_to_tensor(dones)

      replay_buffer.push(obs, logits,
                         rewards, next_obs, dones)

      if len(replay_buffer) > args.batch_size and training and update_counter > args.update_interval:
        update_counter = 0
        for j in range(agents.n_agents):
          sample = replay_buffer.sample(
              args.batch_size, USE_CUDA, norm_rews=True)
          agents.update(sample, j, logger=writer)
        agents.update_all_targets(writer)

      obs = next_obs
      update_counter += 1
      steps += 1
      tot_comms += infos['comms']
      tot_reward = tot_reward + rewards.squeeze().numpy()

    return tot_reward, tot_comms, steps


'''def plot_rewards(argsmreward_history, comm_history):
  plt.plot(reward_history)
  plt.xlabel('Episode')
  plt.ylabel('Average reward per agent')
  plt.savefig(figure_path + 'reward_history.png')
  plt.close()

  plt.plot(comm_history)
  plt.xlabel('Episode')
  plt.ylabel('Communication-savings per episode')
  plt.savefig(figure_path + 'comm_history.png')
  plt.close()
'''


def parse_args():
  parser = argparse.ArgumentParser()
  parser.add_argument('-l', '--load', type=str)
  parser.add_argument('-n', '--n_agents', type=int, default=3)
  parser.add_argument('-b', '--buffer_size', type=int, default=1e6)
  parser.add_argument('-r', '--run_name', type=str, default="default")
  parser.add_argument('-s', '--save', action='store_true')

  parser.add_argument('--model_path', type=str, default="models/")
  parser.add_argument('--figure_path', type=str, default="figures/")
  parser.add_argument('--n_episodes', type=int, default=4000000)
  parser.add_argument('--eval_episodes', type=int, default=5)
  parser.add_argument('--eval_interval', type=int, default=100)
  parser.add_argument('--save_interval', type=int, default=1000)
  parser.add_argument('--full_comm', action='store_true')
  parser.add_argument('--experiment_name', type=str, default="default")

  #Agent config
  parser.add_argument('--lr', type=float, default=1e-5)
  parser.add_argument('--tau', type=float, default=5e-2)
  parser.add_argument('-e', '--epsilon', type=float, default=1)
  parser.add_argument('--epsilon_decay', type=float, default=1e6)
  parser.add_argument('--gamma', type=float, default=0.95)
  parser.add_argument('--actor_hidden_dim', type=int, default=256)
  parser.add_argument('--critic_hidden_dim', type=int, default=256)

  parser.add_argument('--batch_size', type=int, default=128)
  parser.add_argument('--update_interval', type=int, default=4000)

  return parser.parse_args()


def eval(env, agents, buffer, eval_episodes, logger):
  comm = 0
  reward = 0
  tot_steps = 0
  for _ in range(eval_episodes):
    r, c, steps = run_episode(env, agents, buffer, args, training=False)
    comm += c
    reward = reward + np.mean(r, axis=0)
    tot_steps += steps

  comm = comm / (tot_steps * args.n_agents)
  comm /= eval_episodes
  comm = 1 - comm
  reward /= eval_episodes

  return reward, comm


if __name__ == '__main__':
  args = parse_args()
  n_agents = args.n_agents
  global writer
  run_dir = create_run_dir(args.experiment_name)
  writer = SummaryWriter(run_dir + '/logs')

  if not os.path.exists(args.model_path):
    os.makedirs(args.model_path)

  if not os.path.exists(args.figure_path):
    os.makedirs(args.figure_path)

  env = simple_spread_c_v2.parallel_env(N=n_agents, penalty_ratio=0.01, full_comm = args.full_comm,
                                        local_ratio=0.5, max_cycles=25, continuous_actions=True)

  obs_dim = env.observation_space('agent_0').shape[0]
  action_dim = flatdim(env.action_space('agent_0'))

  replay_buffer = ReplayBuffer(int(args.buffer_size), num_agents=n_agents,
                               obs_dims=[obs_dim for _ in range(n_agents)],
                               ac_dims=[action_dim for _ in range(n_agents)])
  if args.load:
    algo = RA_MADDPG.init_from_save(args.load, device=device)
  else:
    algo = RA_MADDPG(in_dim=obs_dim, out_dim=action_dim,
                     n_agents=n_agents,
                     actor_hidden_dim=args.actor_hidden_dim,
                     critic_hidden_dim=args.actor_hidden_dim,
                     discrete_action=False,
                     eps=args.epsilon,
                     eps_decay=args.epsilon_decay,
                     device=device,
                     gamma=args.gamma, lr=args.lr, tau=args.tau)

  best = float('-inf')
  eval_counter = 0
  for i in range(args.n_episodes):
    tot_reward, comms, steps = run_episode(
        env, algo, replay_buffer, args, training=True)

    comm_savings = 1 - (comms / (steps * n_agents))
    avg_reward = sum(tot_reward)/n_agents

    writer.add_scalar('agent/reward', avg_reward, i)
    writer.add_scalar('agent/comm_savings', comm_savings, i)

    if i % args.eval_interval == 0:
      print(f'Episode: {i}/{args.n_episodes}, best performance: {best}')
      eval_reward, eval_comm = eval(
          env, algo, replay_buffer, args.eval_episodes, writer)
      writer.add_scalar('agent/eval_reward', eval_reward, eval_counter)
      writer.add_scalar('agent/eval_comm_savings', eval_comm, eval_counter)
      eval_counter += 1

      if eval_reward >= best:
        best = eval_reward
        if args.save:
          algo.save(run_dir + '/models/best.pt')

  writer.close()
  env.close()
