
import gymnasium as gym
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv
import os
import numpy as np
from hft_gym_env import HftEnv
import argparse

# Augmented Dataset
AUGMENTED_PATH = 'research/data/hbt_multiproduct/TXFB6.npy'
MODEL_DIR = 'research/rl/models'
os.makedirs(MODEL_DIR, exist_ok=True)

def train():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bonus", type=float, default=1.0, help="Trading Bonus")
    # Low entropy for robust/survival mode
    parser.add_argument("--ent_coef", type=float, default=0.01, help="Entropy Coefficient") 
    parser.add_argument("--lr", type=float, default=0.0003, help="Learning Rate")
    parser.add_argument("--gamma", type=float, default=0.99, help="Discount Factor")
    parser.add_argument("--run_name", type=str, default="ppo_hft_robust_agent_v4aug", help="Output Model Name")
    parser.add_argument("--steps", type=int, default=100000, help="Total Timesteps")
    args = parser.parse_args()

    print(f"Checking for Augmented Data at {AUGMENTED_PATH}...")
    if not os.path.exists(AUGMENTED_PATH):
        print(f"Data not found: {AUGMENTED_PATH}. Please run generate_augmented_dataset.py first.")
        # Fallback to verify logic? No, just fail.
        return

    # Create Env
    # Note: HftEnv needs to be able to handle the new V4 features (Thermodynamics).
    # We might need to update HftEnv observation space if V4 added columns.
    # Alpha features v4 likely has MORE columns than v2/v3.
    # The HftEnv usually infers shape from data.shape[1].
    
    print("Initializing HftEnv with Augmented Data...")
    env_kwargs = {'trading_bonus': args.bonus}
    env = HftEnv(AUGMENTED_PATH, **env_kwargs)
    env = DummyVecEnv([lambda: env])
    
    print(f"Initializing Robust PPO Agent: Ent={args.ent_coef} (Low for Stability)")
    
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
    
    print(f"Training for {args.steps} steps on Augmented Data (Original + Jitter + Warp + Crash)...")
    model.learn(total_timesteps=args.steps) 
    
    save_path = f"{MODEL_DIR}/{args.run_name}"
    print(f"Saving Robust Agent to {save_path}...")
    model.save(save_path)
    
    # Evaluation on a "Crash" slice?
    # The env randomly iterates. 
    # For robust verification, we might want to manually inspect later.
    
    print("Training Complete.")

if __name__ == '__main__':
    train()
