
import gymnasium as gym
from gymnasium import spaces
import numpy as np
import os

class HftEnv(gym.Env):
    """
    Custom Environment that follows gymnasium interface.
    Replays HBT data and simulates execution.
    Target: 95% Win Rate (Scalping).
    """
    metadata = {'render.modes': ['human']}

    def __init__(
        self,
        data_path,
        alpha_signal_func=None,
        window_size=40,
        trading_bonus=1.0,
        registry_feature_provider=None,
    ):
        super(HftEnv, self).__init__()
        
        self.trading_bonus = trading_bonus
        self.registry_feature_provider = registry_feature_provider
        
        # Load Data
        print(f"Loading {data_path}...")
        self.data_path = data_path
        
        # Load NPZ or NPY
        loaded = np.load(data_path)
        if isinstance(loaded, np.lib.npyio.NpzFile):
            self.raw_data = loaded['data'] # 2D Array or Structured
        else:
            self.raw_data = loaded
            
        # Detect if structured
        if self.raw_data.dtype.names:
            ev = self.raw_data['ev']
            px = self.raw_data['px']
            timestamps = self.raw_data['exch_ts']
        else:
            ev = self.raw_data[:, 0]
            timestamps = self.raw_data[:, 1]
            px = self.raw_data[:, 4]
        
        mask = (ev == 2)
        self.prices = px[mask].astype(np.float32)
        self.timestamps = timestamps[mask].astype(np.uint64)
        
        # Load Precomputed Features
        # Cycle 12: Auto-detect V5 (Ultimate) -> v3 (Physics/KAN) -> v2 -> v1
        feat_path_v5 = data_path.replace('.npy', '_features_v5.npy')
        feat_path_v3 = data_path.replace('.npy', '_features_v3.npy')
        feat_path_v2 = data_path.replace('.npy', '_features_v2.npy')
        feat_path_v1 = data_path.replace('.npy', '_features.npy')
        
        if os.path.exists(feat_path_v5):
             feat_path = feat_path_v5
             print(f"Loading V5 Features (Ultimate Integration) from {feat_path}...")
        elif os.path.exists(feat_path_v3):
             feat_path = feat_path_v3
             print(f"Loading V3 Features (Physics/KAN) from {feat_path}...")
        elif os.path.exists(feat_path_v2):
             feat_path = feat_path_v2
             print(f"Loading V2 Features from {feat_path} (Cycle 10)...")
        else:
             feat_path = feat_path_v1
             print(f"Loading V1 Features from {feat_path}...")

        try:
            self.features = np.load(feat_path)
            # Cycle 10: Alignment Check
            # If features len != timestamp len, assume trades mask logic
            if len(self.features) != len(self.timestamps):
                 # Assume mask if ratio is close, else error?
                 # Actually, generate_training_features aligned it to Trades (ev=2).
                 # self.timestamps is also masked by ev=2.
                 # So lengths SHOULD match.
                 if len(self.features) == len(self.prices):
                      print("Features aligned with Trades.")
                 else:
                      print(f"Warning: Feature len {len(self.features)} != Trade len {len(self.prices)}. Truncating to min.")
                      min_len = min(len(self.features), len(self.prices))
                      self.features = self.features[:min_len]
                      self.prices = self.prices[:min_len]
                      self.timestamps = self.timestamps[:min_len]
            
        except Exception as e:
            print(f"Feature load failed: {e}. using zeros.")
            # Default to 8 dim (V1) or 10 dim? 
            # Safer to default to 8 for backward compat unless specified.
            self.features = np.zeros((len(self.prices), 8), dtype=np.float32)
            
        # Observation: [Alpha_Features(N), Registry_Alpha_Features(M), Inventory]
        # Features map V2: [Existing(8), MicroPrice, OFI_I]
        self.feature_dim = self.features.shape[1]
        self.registry_dim = (
            len(getattr(self.registry_feature_provider, "feature_names", ()))
            if self.registry_feature_provider is not None
            else 0
        )
        self.obs_dim = self.feature_dim + self.registry_dim + 1  # Features + Registry + Inv
        print(f"Observation Dim: {self.obs_dim} ({self.feature_dim} features)")
        
        self.observation_space = spaces.Box(low=-np.inf, high=np.inf, shape=(self.obs_dim,), dtype=np.float32)
        
        # Actions: Maker (5) - Hold, BuyLimit, SellLimit, BuyMkt, SellMkt
        self.action_space = spaces.Discrete(5)
        
        self.idx = 0
        self.max_steps = len(self.prices) - 100
        self.inventory = 0
        self.cash = 0.0
        self.entry_price = 0.0
        
        # Risk Constants
        self.max_pos = 1
        self.fee_rate = 0.0 # Start with 0 (Curriculum) for now, manageable via wrapper. 

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self.idx = 0
        self.inventory = 0
        self.cash = 0.0
        self.entry_price = 0.0
        if self.registry_feature_provider is not None and hasattr(self.registry_feature_provider, "reset"):
            self.registry_feature_provider.reset()
        return self._get_obs(), {}

    def _get_obs(self):
        # Extract features
        if self.idx >= len(self.features):
            return np.zeros(self.obs_dim, dtype=np.float32)
            
        feats = self.features[self.idx]
        registry_feats = np.zeros(self.registry_dim, dtype=np.float32)
        if self.registry_feature_provider is not None and self.registry_dim > 0:
            payload = {
                "current_mid": float(self.prices[self.idx]),
                "bid_px": float(self.prices[self.idx]),
                "ask_px": float(self.prices[self.idx]),
                "trade_vol": 1.0,
            }
            try:
                sig = self.registry_feature_provider.update(**payload)
                arr = np.asarray(sig, dtype=np.float32).reshape(-1)
                n = min(arr.size, self.registry_dim)
                if n > 0:
                    registry_feats[:n] = arr[:n]
            except Exception:
                # Keep zeros when provider update fails.
                pass
        obs = np.concatenate([feats, registry_feats, [self.inventory]])
        return obs.astype(np.float32)

    def step(self, action):
        current_price = self.prices[self.idx]
        
        # Action Map:
        # 0: Hold
        # 1: Buy Limit (Best Bid)
        # 2: Sell Limit (Best Ask)
        # 3: Buy Market (Aggressive)
        # 4: Sell Market (Aggressive)
        
        reward = 0.0
        terminated = False
        
        trade_pnl = 0.0
        executed = False
        fill_price = 0.0
        fee_paid = 0.0
        
        # execution logic
        if action == 3: # Buy Mkt
            if self.inventory < self.max_pos:
                fill_price = current_price # Slip?
                self.inventory += 1
                self.cash -= fill_price * (1 + self.fee_rate)
                self.entry_price = fill_price
                executed = True
                
        elif action == 4: # Sell Mkt
            if self.inventory > -self.max_pos:
                fill_price = current_price
                self.inventory -= 1
                self.cash += fill_price * (1 - self.fee_rate)
                self.entry_price = fill_price
                executed = True
                
        if action == 1: # Buy Limit
            # Relaxed Logic: Fill at Current Price if Next Price <= Current
            # Basically a Market Order but we call it Limit to test flow
            limit_px = current_price 
            
            # Peek next
            if self.idx + 1 < len(self.prices):
                next_p = self.prices[self.idx+1]
                # Debug
                if self.idx < 1000 and self.idx % 200 == 0:
                     print(f"Debug Fill: Act=1, Lim={limit_px}, Next={next_p}")
                
                if next_p <= limit_px:
                     if self.inventory < self.max_pos:
                        fill_price = limit_px
                        self.inventory += 1
                        self.cash -= fill_price 
                        self.entry_price = fill_price
                        executed = True

        elif action == 2: # Sell Limit
            limit_px = current_price 
            if self.idx + 1 < len(self.prices):
                next_p = self.prices[self.idx+1]
                if next_p >= limit_px:
                    if self.inventory > -self.max_pos:
                        fill_price = limit_px
                        self.inventory -= 1
                        self.cash += fill_price
                        self.entry_price = fill_price
                        executed = True

        # Mark to Market
        portfolio_value = self.cash + (self.inventory * current_price)
        
        # Advance
        self.idx += 1
        if self.idx >= self.max_steps or self.idx >= len(self.prices):
            terminated = True
            
        # Calc Reward (Sharpe-like)
        # PnL Change
        next_price = self.prices[self.idx] if self.idx < len(self.prices) else current_price
        next_value = self.cash + (self.inventory * next_price)
        step_reward = next_value - portfolio_value
        
        # Reward Shaping: Trading Bonus
        # If executed, give small positive reward to encourage action
        # This breaks the "Zero-Trade" local optimum
        if executed:
            step_reward += self.trading_bonus
        
        # Penalty for Inventory Risk (High Inventory when Volatility is high?)
        # step_reward -= 0.1 * abs(self.inventory) * vol?
        
        reward = step_reward
        
        info = {
            "pnl": next_value,
            "price": next_price,
            "action": action,
            "executed": executed
        }
        
        return self._get_obs(), reward, terminated, False, info
