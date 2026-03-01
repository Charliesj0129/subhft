
import gymnasium as gym
from stable_baselines3 import PPO
import numpy as np
import os
from hft_gym_env import HftEnv
from structlog import get_logger

DATA_PATH = 'research/data/hbt_multiproduct/TXFB6.npy'
MODEL_PATH = "research/rl/ppo_hft_agent"

def evaluate():
    if not os.path.exists(DATA_PATH):
        print(f"Data not found: {DATA_PATH}")
        return

    # Create Env
    env = HftEnv(DATA_PATH)
    env.fee_rate = 0.0 # Force Trading Check
    
    # Maker Agent Path
    MODEL_PATH = "research/rl/ppo_hft_maker_agent"
    print(f"Loading Model from {MODEL_PATH}...")
    try:
        model = PPO.load(MODEL_PATH)
    except Exception as e:
        print(f"Failed to load model: {e}")
        return

    print("Running Evaluation (Debug Mode)...")
    obs, _ = env.reset()
    
    trades = 0
    wins = 0
    losses = 0
    total_pnl = 0.0
    prev_inv = 0
    entry_price = 0.0
    
    # Run loop
    step_count = 0
    while True:
        action, _states = model.predict(obs, deterministic=True)
        obs, reward, terminated, truncated, info = env.step(action)
        
        step_count += 1
        curr_inv = env.inventory
        price = info['price']
        
        trade_occurred = (curr_inv != prev_inv)
        
        if trade_occurred:
            if prev_inv == 0:
                entry_price = price
            elif curr_inv == 0:
                pnl = 0
                if prev_inv == 1:
                    pnl = price - entry_price
                elif prev_inv == -1:
                    pnl = entry_price - price
                
                fees = (entry_price + price) * 0.00005
                net_pnl = pnl - fees
                total_pnl += net_pnl
                trades += 1
                if net_pnl > 0:
                    wins += 1
                else:
                    losses += 1
                    
        prev_inv = curr_inv
        
        if step_count % 1000 == 0:
            print(f"Step {step_count}: Trades={trades}, Wins={wins}, PnL={total_pnl:.2f}")

        if terminated or truncated:
            break
            
    print("-" * 30)
    print(f"Total Trades: {trades}")
    if trades > 0:
        win_rate = wins / trades
        print(f"Win Rate: {win_rate:.4f} ({win_rate*100:.2f}%)")
    print(f"Total PnL: {total_pnl:.2f}")

if __name__ == '__main__':
    evaluate()
