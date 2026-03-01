
import gymnasium as gym
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv
import os
from hft_gym_env import HftEnv

DATA_PATH = 'research/data/hbt_multiproduct/TXFB6.npy'

import argparse

def train():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bonus", type=float, default=1.0, help="Trading Bonus per fill")
    parser.add_argument("--ent_coef", type=float, default=0.2, help="Entropy Coefficient")
    parser.add_argument("--lr", type=float, default=0.0003, help="Learning Rate")
    parser.add_argument("--gamma", type=float, default=0.99, help="Discount Factor")
    parser.add_argument("--run_name", type=str, default="ppo_hft_maker_agent", help="Output Model Name")
    args = parser.parse_args()

    # check data exists
    if not os.path.exists(DATA_PATH):
        print(f"Data not found: {DATA_PATH}")
        return

    # Create Env with Sweep Params
    # Dictionary passed to Env
    env_kwargs = {'trading_bonus': args.bonus}
    
    # We need to update HftEnv to accept kwargs if not already
    env = HftEnv(DATA_PATH, **env_kwargs)
    env = DummyVecEnv([lambda: env])
    
    print(f"Initializing PPO Agent: Bonus={args.bonus}, Ent={args.ent_coef}, LR={args.lr}, Gamma={args.gamma}")
    
    model = PPO(
        "MlpPolicy", 
        env, 
        verbose=1, 
        learning_rate=args.lr, 
        n_steps=2048, 
        ent_coef=args.ent_coef, 
        gamma=args.gamma,
        batch_size=256
    )
    
    print("Curriculum Training (Phase 1: Zero Fee)...")
    # Fee is 0.0 inside Env by default now
    model.learn(total_timesteps=50000) 
    
    # Optional: Phase 2 with Fee? For Sweep speed, maybe just 50k steps is enough to compare.
    # print("Curriculum Training (Phase 2: Low Fee)...")
    # env.envs[0].fee_rate = 0.00001
    # model.learn(total_timesteps=50000)
    
    print(f"Saving model to research/rl/{args.run_name}...")
    model.save(f"research/rl/{args.run_name}")
    
    # Evaluation
    print("Evaluating...")
    obs = env.reset()
    total_reward = 0.0
    steps = 1000
    
    # Run evaluation episode
    for i in range(steps):
        action, _states = model.predict(obs, deterministic=True)
        obs, rewards, dones, info = env.step(action)
        total_reward += rewards[0] # VecEnv returns array
        
        if dones[0]:
            obs = env.reset()
            
    print(f"Evaluation Complete. Total Reward: {total_reward:.4f}")
    # Print Machine Readable Result for Optuna
    print(f"RESULT: {total_reward:.4f}")

if __name__ == '__main__':
    train()
