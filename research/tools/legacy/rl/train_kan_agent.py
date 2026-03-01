
import gymnasium as gym
from stable_baselines3 import PPO
from stable_baselines3.common.torch_layers import BaseFeaturesExtractor
from stable_baselines3.common.vec_env import DummyVecEnv
import torch
import torch.nn as nn
import os
from hft_gym_env import HftEnv
from kan import KAN

# Define Custom Feature Extractor for SB3
class KANExtractor(BaseFeaturesExtractor):
    """
    Feature extractor that uses a Kolmogorov-Arnold Network (KAN)
    instead of a standard MLP.
    """
    def __init__(self, observation_space: gym.spaces.Box, features_dim: int = 64):
        # Flatten observation space
        input_dim = observation_space.shape[0] if len(observation_space.shape) > 0 else 1
        super().__init__(observation_space, features_dim)
        
        # Define KAN Network
        # Input -> Hidden -> Features
        self.kan = KAN(
            layers_hidden=[input_dim, 128, features_dim],
            grid_size=5,
            spline_order=3,
        )

    def forward(self, observations: torch.Tensor) -> torch.Tensor:
        return self.kan(observations)

DATA_PATH = 'research/data/hbt_multiproduct/TXFB6.npy'

def train_kan():
    # check data exists
    if not os.path.exists(DATA_PATH):
        print(f"Data not found: {DATA_PATH}. Please ensure training data exists.")
        return

    # Initialize Env
    env = HftEnv(DATA_PATH)
    env = DummyVecEnv([lambda: env])
    
    print("Initializing PPO Agent with KAN Brain...")
    
    # Custom Policy Configuration
    policy_kwargs = dict(
        features_extractor_class=KANExtractor,
        features_extractor_kwargs=dict(features_dim=64),
        # After extractor, SB3 usually adds 2 layers of MLP for Pi and VF.
        # We can keep those standard or replace them too?
        # For this demo, we replace the "Feature Extractor" (Shared body) with KAN.
        net_arch=dict(pi=[32], vf=[32]) 
    )
    
    model = PPO(
        "MlpPolicy", 
        env, 
        verbose=1, 
        learning_rate=3e-4, 
        policy_kwargs=policy_kwargs,
        # tensorboard_log="./research/rl/kan_tensorboard/"
    )
    
    print("Model Architecture:")
    print(model.policy)
    
    print("Training KAN Agent (Short Run)...")
    model.learn(total_timesteps=10000) 
    
    # Save
    save_path = "research/rl/ppo_kan_agent"
    print(f"Saving to {save_path}...")
    model.save(save_path)
    
    print("KAN Training Complete.")

if __name__ == '__main__':
    train_kan()
