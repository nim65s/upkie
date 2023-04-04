#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright 2023 Inria

import gym
import numpy as np

import upkie_locomotion.envs

if __name__ == "__main__":
    upkie_locomotion.envs.register()
    env = gym.make("UpkieLegsEnv-v1")
    observation = env.reset(seed=42)

    action = 0.0 * env.action_space.sample()
    for step in range(1_000_000):
        observation, reward, done, _ = env.step(action)
        if done:
            observation = env.reset()
        pitch = observation[0]
        action[2] = np.nan
        action[6 + 2] = 200.0 * pitch
        action[5] = np.nan
        action[6 + 5] = -200.0 * pitch

    env.close()