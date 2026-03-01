
import gymnasium as gym
from stable_baselines3 import PPO
import numpy as np
import os
from hft_gym_env import HftEnv

DATA_PATH = 'research/data/hbt_multiproduct/TXFB6.npy'
MODEL_PATH = "research/rl/ppo_hft_agent"

def evaluate():
    if not os.path.exists(DATA_PATH):
        print(f"Data not found: {DATA_PATH}")
        return

    # Create Env
    env = HftEnv(DATA_PATH)
    
    print(f"Loading Model from {MODEL_PATH}...")
    try:
        model = PPO.load(MODEL_PATH)
    except Exception as e:
        print(f"Failed to load model: {e}")
        return

    print("Running Evaluation...")
    obs, _ = env.reset()
    
    trades = 0
    wins = 0
    losses = 0
    total_pnl = 0.0
    
    # Track inventory to detect trades
    prev_inv = 0
    
    # We need to track realized PnL per trade to determine "Win"
    # The Env returns 'reward' which is step_reward (mark to market).
    # We can approximate "Win" by checking if Step Reward > 0 when Inventory changes?
    # No, MTM fluctuates.
    # We need to track Entry/Exit.
    
    # Simulating simple trade tracking
    entry_price = 0.0
    position = 0 # 1, 0, -1
    
    # Run until done
    while True:
        action, _states = model.predict(obs, deterministic=True) # Deterministic for Eval
        obs, reward, terminated, truncated, info = env.step(action)
        
        # Check Trade
        # Env action: 1=Buy, 2=Sell
        # Env inventory logic:
        # If Action=1 and inv < max: Buy
        # If Action=2 and inv > -max: Sell
        
        # We can inspect env.inventory?
        curr_inv = env.inventory
        price = info['price']
        
        # Detect Trade Execution
        trade_occurred = (curr_inv != prev_inv)
        
        if trade_occurred:
            # We either Opened or Closed/Flipped
            # For HFT scalping (Max=1), we toggle 0 -> 1/ -1 -> 0
            
            # Case 1: Open (0 -> 1 or 0 -> -1)
            if prev_inv == 0:
                entry_price = price
                position = curr_inv
                
            # Case 2: Close (1 -> 0 or -1 -> 0)
            elif curr_inv == 0:
                # Realized
                pnl = 0
                if prev_inv == 1: # Sold to close
                    pnl = price - entry_price
                elif prev_inv == -1: # Bought to close
                    pnl = entry_price - price
                    
                # Fee
                # 2 * fee_rate approx
                # In env, cash tracks fees. 
                # Let's trust the PnL? 
                # But we want "Win Rate".
                # Gross PnL > fees?
                fees = (entry_price + price) * 0.00005
                net_pnl = pnl - fees
                
                total_pnl += net_pnl
                trades += 1
                if net_pnl > 0:
                    wins += 1
                else:
                    losses += 1
                    
            # Case 3: Flip (1 -> -1) - Not supported by single step in this env logic (step size 1)
            # Env only changes by 1 unit.
        
        prev_inv = curr_inv
        
        if terminated or truncated:
            break
            
    print("-" * 30)
    print(f"Total Trades: {trades}")
    print(f"Wins: {wins}")
    print(f"Losses: {losses}")
    if trades > 0:
        win_rate = wins / trades
        print(f"Win Rate: {win_rate:.4f} ({win_rate*100:.2f}%)")
    else:
        print("Win Rate: N/A (No Trades)")
    print(f"Total PnL: {total_pnl:.2f}")
    print("-" * 30)

if __name__ == '__main__':
    evaluate()
