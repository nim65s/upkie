#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2023 Inria
# SPDX-License-Identifier: Apache-2.0

import argparse
import datetime
import os
import random
import signal
import tempfile
from typing import Dict, List

import gin
import gymnasium
import numpy as np
import stable_baselines3
import yaml
from envs import make_ppo_balancer_env
from rules_python.python.runfiles import runfiles
from schedules import affine_schedule
from settings import EnvSettings, PPOSettings, SACSettings
from stable_baselines3.common.callbacks import BaseCallback, CheckpointCallback
from stable_baselines3.common.logger import TensorBoardOutputFormat
from stable_baselines3.common.monitor import Monitor
from stable_baselines3.common.utils import set_random_seed
from stable_baselines3.common.vec_env import (
    DummyVecEnv,
    SubprocVecEnv,
    VecNormalize,
)
from stable_baselines3.common.vec_env.base_vec_env import VecEnv
from torch import nn
from utils import gin_operative_config_dict

import upkie.envs
from upkie.envs import InitRandomization
from upkie.utils.spdlog import logging

upkie.envs.register()


def parse_command_line_arguments() -> argparse.Namespace:
    """
    Parse command line arguments.

    Returns:
        Command-line arguments.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--name",
        default="",
        type=str,
        help="name of the new policy to train",
    )
    parser.add_argument(
        "--nb-envs",
        default=1,
        type=int,
        help="number of parallel simulation processes to run",
    )
    parser.add_argument(
        "--show",
        default=False,
        action="store_true",
        help="show simulator during trajectory rollouts",
    )
    return parser.parse_args()


class InitRandomizationCallback(BaseCallback):
    def __init__(
        self,
        vec_env: VecEnv,
        key: str,
        max_value: float,
        start_timestep: int,
        end_timestep: int,
    ):
        super().__init__()
        self.end_timestep = end_timestep
        self.key = key
        self.max_value = max_value
        self.start_timestep = start_timestep
        self.vec_env = vec_env

    def _on_step(self) -> bool:
        progress: float = np.clip(
            (self.num_timesteps - self.start_timestep) / self.end_timestep,
            0.0,
            1.0,
        )
        cur_value = progress * self.max_value
        self.vec_env.env_method("update_init_rand", **{self.key: cur_value})
        self.logger.record(f"init_rand/{self.key}", cur_value)


class SummaryWriterCallback(BaseCallback):
    def __init__(self, vec_env: VecEnv, save_path: str):
        super().__init__()
        self.save_path = save_path
        self.vec_env = vec_env

    def _on_training_start(self):
        output_formats = self.logger.output_formats
        self.tb_formatter = next(
            formatter
            for formatter in output_formats
            if isinstance(formatter, TensorBoardOutputFormat)
        )

    def _on_step(self) -> bool:
        # We wait for the first call to log operative config so that parameters
        # for functions called by the environment are logged as well.
        if self.n_calls != 1:
            return
        env_settings = EnvSettings()
        config = {
            "env": env_settings.env_id,
            "gin": gin_operative_config_dict(gin.config._OPERATIVE_CONFIG),
            "spine_config": env_settings.spine_config,
        }
        self.tb_formatter.writer.add_text(
            "config",
            f"```yaml\n{yaml.dump(config, indent=4)}\n```",
            global_step=None,
        )
        save_path = f"{self.save_path}/config.yaml"
        with open(save_path, "w") as fh:
            yaml.dump(config, fh, indent=4)
        logging.info(f"Saved configuration to {save_path}")


def get_random_word():
    with open("/usr/share/dict/words") as fh:
        words = fh.read().splitlines()
    word_index = random.randint(0, len(words))
    while not words[word_index].isalnum():
        word_index = (word_index + 1) % len(words)
    return words[word_index]


def get_bullet_argv(shm_name: str, show: bool) -> List[str]:
    """!
    Get command-line arguments for the Bullet spine.

    @param shm_name Name of the shared-memory file.
    @param show If true, show simulator GUI.
    @returns Command-line arguments.
    """
    env_settings = EnvSettings()
    agent_frequency = env_settings.agent_frequency
    spine_frequency = env_settings.spine_frequency
    assert spine_frequency % agent_frequency == 0
    nb_substeps = spine_frequency / agent_frequency
    bullet_argv = []
    bullet_argv.extend(["--shm-name", shm_name])
    bullet_argv.extend(["--nb-substeps", str(nb_substeps)])
    bullet_argv.extend(["--spine-frequency", str(spine_frequency)])
    if show:
        bullet_argv.append("--show")
    return bullet_argv


def init_env(
    max_episode_duration: float,
    show: bool,
    spine_path: str,
):
    """!
    Get an environment initialization function for a set of parameters.

    @param max_episode_duration Maximum duration of an episode, in seconds.
    @param show If true, show simulator GUI.
    @param spine_path Path to the Bullet spine binary.
    """
    env_settings = EnvSettings()
    seed = random.randint(0, 1_000_000)

    def _init():
        shm_name = f"/{get_random_word()}"
        pid = os.fork()
        if pid == 0:  # child process: spine
            argv = get_bullet_argv(shm_name, show=show)
            os.execvp(spine_path, ["bullet"] + argv)
            return

        # parent process: trainer
        agent_frequency = env_settings.agent_frequency
        velocity_env = gymnasium.make(
            env_settings.env_id,
            max_episode_steps=int(max_episode_duration * agent_frequency),
            frequency=agent_frequency,
            regulate_frequency=False,
            reward_weights=upkie.envs.UpkieGroundVelocity.RewardWeights(
                **env_settings.reward_weights
            ),
            shm_name=shm_name,
            spine_config=env_settings.spine_config,
            max_ground_velocity=env_settings.max_ground_velocity,
        )
        velocity_env.reset(seed=seed)
        velocity_env._prepatch_close = velocity_env.close

        def close_monkeypatch():
            logging.info(f"Terminating spine {shm_name} with {pid=}...")
            os.kill(pid, signal.SIGINT)  # interrupt spine child process
            os.waitpid(pid, 0)  # wait for spine to terminate
            velocity_env._prepatch_close()

        velocity_env.close = close_monkeypatch
        return Monitor(make_ppo_balancer_env(velocity_env, training=True))

    set_random_seed(seed)
    return _init


def find_save_path(training_dir: str, policy_name: str):
    def path_for_iter(nb_iter: int):
        return f"{training_dir}/{policy_name}_{nb_iter}"

    nb_iter = 1
    while os.path.exists(path_for_iter(nb_iter)):
        nb_iter += 1
    return path_for_iter(nb_iter)


@gin.configurable
def train_policy(
    policy_name: str,
    training_dir: str,
    nb_envs: int,
    show: bool,
    init_rand: Dict[str, float],
    max_episode_duration: float,
    return_horizon: float,
    total_timesteps: int,
) -> None:
    """!
    Train a new policy and save it to a directory.

    @param policy_name Name of the trained policy.
    @param training_dir Directory for logging and saving policies.
    @param nb_envs Number of environments, each running in a separate process.
    @param show Whether to show the simulation GUI.
    """
    if policy_name == "":
        policy_name = get_random_word()
    save_path = find_save_path(training_dir, policy_name)
    logging.info('New policy name is "%s"', policy_name)
    logging.info("Training data will be logged to %s", save_path)

    deez_runfiles = runfiles.Create()
    spine_path = os.path.join(
        agent_dir,
        deez_runfiles.Rlocation("upkie/spines/bullet_spine"),
    )

    vec_env = (
        SubprocVecEnv(
            [
                init_env(
                    max_episode_duration=max_episode_duration,
                    show=show,
                    spine_path=spine_path,
                )
                for i in range(nb_envs)
            ],
            start_method="fork",
        )
        if nb_envs > 1
        else DummyVecEnv(
            [
                init_env(
                    max_episode_duration=max_episode_duration,
                    show=show,
                    spine_path=spine_path,
                )
            ]
        )
    )

    # call make_ppo_balancer_env once to update the logged gin config
    # (otherwise done in child processes, the config wouldn't be fully logged)
    make_ppo_balancer_env(vec_env, training=True)

    if False:  # does not always improve returns during training
        vec_env = VecNormalize(vec_env)

    env_settings = EnvSettings()
    dt = 1.0 / env_settings.agent_frequency
    gamma = 1.0 - dt / return_horizon
    logging.info(
        "Discount factor gamma=%f for a return horizon of %f s",
        gamma,
        return_horizon,
    )

    algorithm = "PPO"
    if algorithm == "PPO":
        ppo_settings = PPOSettings()
        policy = stable_baselines3.PPO(
            "MlpPolicy",
            vec_env,
            learning_rate=affine_schedule(
                y_1=ppo_settings.learning_rate,  # progress_remaining=1.0
                y_0=ppo_settings.learning_rate / 3,  # progress_remaining=0.0
            ),
            # exponential_decay_schedule(
            #     ppo_settings.learning_rate,
            #     nb_phases=2,
            # ),
            n_steps=ppo_settings.n_steps,
            batch_size=ppo_settings.batch_size,
            n_epochs=ppo_settings.n_epochs,
            gamma=gamma,
            gae_lambda=ppo_settings.gae_lambda,
            clip_range=ppo_settings.clip_range,
            clip_range_vf=ppo_settings.clip_range_vf,
            normalize_advantage=ppo_settings.normalize_advantage,
            ent_coef=ppo_settings.ent_coef,
            vf_coef=ppo_settings.vf_coef,
            max_grad_norm=ppo_settings.max_grad_norm,
            use_sde=ppo_settings.use_sde,
            sde_sample_freq=ppo_settings.sde_sample_freq,
            target_kl=ppo_settings.target_kl,
            tensorboard_log=training_dir,
            policy_kwargs={
                "activation_fn": nn.Tanh,
                "net_arch": dict(
                    pi=ppo_settings.net_arch_pi,
                    vf=ppo_settings.net_arch_vf,
                ),
            },
            verbose=1,
        )
    elif algorithm == "SAC":
        sac_settings = SACSettings()
        action_noise = (
            stable_baselines3.NormalActionNoise(
                mean=np.zeros(1),
                sigma=sac_settings.action_noise * np.ones(1),
            )
            if sac_settings.action_noise is not None
            else None
        )
        policy = stable_baselines3.SAC(
            "MlpPolicy",
            vec_env,
            learning_rate=sac_settings.learning_rate,
            buffer_size=sac_settings.buffer_size,
            learning_starts=sac_settings.learning_starts,
            batch_size=sac_settings.batch_size,
            tau=sac_settings.tau,
            gamma=gamma,
            train_freq=sac_settings.train_freq,
            gradient_steps=sac_settings.gradient_steps,
            action_noise=action_noise,
            optimize_memory_usage=sac_settings.optimize_memory_usage,
            ent_coef=sac_settings.ent_coef,
            target_update_interval=sac_settings.target_update_interval,
            target_entropy=sac_settings.target_entropy,
            use_sde=sac_settings.use_sde,
            sde_sample_freq=sac_settings.sde_sample_freq,
            use_sde_at_warmup=sac_settings.use_sde_at_warmup,
            stats_window_size=sac_settings.stats_window_size,
            tensorboard_log=training_dir,
            policy_kwargs={
                "activation_fn": nn.ReLU,
                "net_arch": dict(
                    pi=sac_settings.net_arch_pi,
                    qf=sac_settings.net_arch_qf,
                ),
            },
            verbose=1,
        )
    else:
        raise Exception(f"Unknown RL algorithm: {algorithm}")

    max_init_rand = InitRandomization(**init_rand)
    try:
        policy.learn(
            total_timesteps=total_timesteps,
            callback=[
                CheckpointCallback(
                    save_freq=max(210_000 // nb_envs, 1_000),
                    save_path=save_path,
                    name_prefix="checkpoint",
                ),
                SummaryWriterCallback(vec_env, save_path),
                InitRandomizationCallback(
                    vec_env,
                    "pitch",
                    max_init_rand.pitch,
                    start_timestep=0,
                    end_timestep=1e5,
                ),
                InitRandomizationCallback(
                    vec_env,
                    "v_x",
                    max_init_rand.v_x,
                    start_timestep=0,
                    end_timestep=1e5,
                ),
                InitRandomizationCallback(
                    vec_env,
                    "omega_y",
                    max_init_rand.omega_y,
                    start_timestep=0,
                    end_timestep=1e5,
                ),
            ],
            tb_log_name=policy_name,
        )
    except KeyboardInterrupt:
        logging.info("Training interrupted.")

    # Save policy no matter what!
    policy.save(f"{save_path}/final.zip")
    policy.env.close()


if __name__ == "__main__":
    args = parse_command_line_arguments()
    agent_dir = os.path.dirname(__file__)
    gin.parse_config_file(f"{agent_dir}/envs.gin")
    gin.parse_config_file(f"{agent_dir}/settings.gin")
    gin.parse_config_file(f"{agent_dir}/train.gin")

    training_path = os.environ.get(
        "UPKIE_TRAINING_PATH", tempfile.gettempdir()
    )
    date = datetime.datetime.now().strftime("%Y-%m-%d")
    training_dir = f"{training_path}/{date}"
    logging.info("Logging training data in %s", training_dir)
    logging.info(
        "To track in TensorBoard, run "
        f"`tensorboard --logdir {training_dir}`"
    )
    train_policy(
        args.name,
        training_dir,
        nb_envs=args.nb_envs,
        show=args.show,
    )
