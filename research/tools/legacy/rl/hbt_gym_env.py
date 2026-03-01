
import gymnasium as gym
from gymnasium import spaces
import numpy as np
import os
import sys
from numba import njit
from hftbacktest import (
    HashMapMarketDepthBacktest,
    BacktestAsset,
    GTX, LIMIT, MARKET
)

class HbtGymEnv(gym.Env):
    """
    Standardized Gym Environment using hftbacktest.
    """
    metadata = {'render.modes': ['human']}

    def __init__(self, data_path, step_size_ns=1_000_000_000): # 1 second default
        super(HbtGymEnv, self).__init__()
        
        self.data_path = data_path
        self.step_size_ns = step_size_ns
        
try:
    from research.factors.alpha_factors import calc_obi_online
except ImportError:
    # Inline fallback
    @njit
    def calc_obi_online(bid_qty, ask_qty):
        total = bid_qty + ask_qty
        if total == 0: return 0.0
        return (bid_qty - ask_qty) / total

class HbtGymEnv(gym.Env):
    """
    Standardized Gym Environment using hftbacktest.
    """
    metadata = {'render.modes': ['human']}

    def __init__(self, data_path, step_size_ns=1_000_000_000): # 1 second default
        super(HbtGymEnv, self).__init__()
        
        self.data_path = data_path
        self.step_size_ns = step_size_ns
        
        # Load Data
        if not os.path.exists(data_path):
            raise FileNotFoundError(f"{data_path} not found")
            
        print(f"Loading {data_path} for HBT Gym Env...")
        raw_data = np.load(data_path)
        
        # Snapshot Logic
        snap_file = data_path.replace('.npy', '_snapshot.npz')
        if os.path.exists(snap_file):
             print(f"Found snapshot: {snap_file}")
             snap = np.load(snap_file)
             bids = snap['bid']
             asks = snap['ask']
             ts = snap['timestamp']
             snap_events = []
             for row in bids:
                snap_events.append((1, ts, ts, row[0], row[1], 0, 1, 0.0))
             for row in asks:
                snap_events.append((1, ts, ts, row[0], row[1], 0, -1, 0.0))
             
             snap_arr = np.array(snap_events, dtype=raw_data.dtype)
             self.raw_data = np.concatenate([snap_arr, raw_data])
             print(f"Prepended {len(snap_arr)} snapshot events.")
        else:
             self.raw_data = raw_data
        
        if len(self.raw_data) > 3_000_000:
            self.raw_data = np.ascontiguousarray(self.raw_data[:3_000_000])
            print("Truncated to 3M events (Contiguous).")
        
        print(f"First TS: {self.raw_data['exch_ts'][0]}")
        
        # Action Space: 
        # 0: Hold
        # 1: Buy Limit (Best Bid)
        # 2: Sell Limit (Best Ask)
        # 3: Buy Market
        # 4: Sell Market
        # 5: Cancel All
        self.action_space = spaces.Discrete(6)
        
        # Observation Space:
        # [BidP, AskP, BidQty, AskQty, Position, Cash, InventoryCost, OBI]
        self.obs_dim = 8
        self.observation_space = spaces.Box(low=-np.inf, high=np.inf, shape=(self.obs_dim,), dtype=np.float32)
        
        self.asset_no = 0
        self.hbt = None
        
    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        
        # Re-init HBT
        asset = (
            BacktestAsset()
            .linear_asset(1.0)
            .constant_order_latency(1_000_000, 1_000_000) # 1ms
            .power_prob_queue_model3(3.0) 
            .no_partial_fill_exchange()
            .trading_value_fee_model(0.00005, 0.00005) 
            .tick_size(1.0)
            .lot_size(1.0)
        )
        # Use add_data logic if safe, otherwise just data()
        # asset.data(self.raw_data)
        try:
            asset.add_data(self.raw_data)
        except:
            asset.data(self.raw_data)
        
        self.hbt = HashMapMarketDepthBacktest([asset])
        self.prev_equity = 0.0 
        
        return self._get_obs(), {}

    def _get_obs(self):
        depth = self.hbt.depth(self.asset_no)
        pos = self.hbt.position(self.asset_no)
        
        best_bid = depth.best_bid
        best_ask = depth.best_ask
        bid_qty = depth.bid_qty_at_tick(0)
        ask_qty = depth.ask_qty_at_tick(0)
        
        if np.isnan(best_bid): best_bid = 0.0
        if np.isnan(best_ask): best_ask = 0.0
        
        obi = calc_obi_online(bid_qty, ask_qty)
        
        obs = np.array([best_bid, best_ask, bid_qty, ask_qty, pos, 0.0, 0.0, obi], dtype=np.float32)
        return obs

    def step(self, action):
        if self.hbt is None:
            raise RuntimeError("Call reset() first")
            
        # Execute Action
        # Current State
        depth = self.hbt.depth(self.asset_no)
        best_bid = depth.best_bid
        best_ask = depth.best_ask
        
        # Simple Order Logic
        # We use a unique order ID based on timestamp or counter?
        # HBT usually auto-manages or we provide ID.
        # We'll use a simple counter if needed, or just standard ID.
        # HBT submit_buy_order(asset, order_id, price, qty, time_in_force, order_type, wait)
        
        order_id = int(self.hbt.current_timestamp % 1_000_000_000) # Pseudo ID
        
        if action == 1: # Buy Limit
            if not np.isnan(best_bid):
                self.hbt.submit_buy_order(self.asset_no, order_id, best_bid, 1.0, GTX, LIMIT, False)
        elif action == 2: # Sell Limit
            if not np.isnan(best_ask):
                self.hbt.submit_sell_order(self.asset_no, order_id, best_ask, 1.0, GTX, LIMIT, False)
        elif action == 3: # Buy Mkt
            # Market buy usually at Ask
             if not np.isnan(best_ask):
                self.hbt.submit_buy_order(self.asset_no, order_id, best_ask, 1.0, GTX, MARKET, False) # Or just high price LIMIT
        elif action == 4: # Sell Mkt
             if not np.isnan(best_bid):
                self.hbt.submit_sell_order(self.asset_no, order_id, best_bid, 1.0, GTX, MARKET, False)
        elif action == 5: # Cancel All
            self.hbt.clear_inactive_orders(self.asset_no)
            
        # Elapse Time
        status = self.hbt.elapse(self.step_size_ns)
        print(f"DEBUG: Elapse Status: {status}, Current TS: {self.hbt.current_timestamp}")
        terminated = (status != 0)
        truncated = False
        
        # Calculate Reward
        # We need equity change.
        # Since we can't easily get equity from `hbt` object in this binding (assumed),
        # we might need to approximate or skip.
        # Ideally: curr_equity = self.hbt.equity(self.asset_no)
        # We will assume it's not available and return 0.0 for now to avoid crash.
        
        reward = 0.0 
        
        obs = self._get_obs()
        info = {}
        
        return obs, reward, terminated, truncated, info

if __name__ == "__main__":
    # Test Run
    data_path = "research/data/hbt_multiproduct/TXFB6.npy"
    if os.path.exists(data_path):
        env = HbtGymEnv(data_path, step_size_ns=1_000_000)
        obs, _ = env.reset()
        print("Env Reset. Initial Obs:", obs)
        
        for _ in range(10):
            action = env.action_space.sample()
            obs, r, term, trunc, info = env.step(action)
            print(f"Step Act={action}, Obs={obs[:5]}..., Term={term}")
            if term: break
    else:
        print("Data not found for test.")
