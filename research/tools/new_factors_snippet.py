
class MarkovTransitionFactor(FactorBase):
    """
    Adaptive Markov Expectation
    Paper: 2601.04959 (Markov Chain Analysis)
    Logic: Learns E[Return(t+1) | State(t)] adaptively. State(t) = Sign(Return(t)).
    """
    
    @property
    def name(self) -> str:
        return "MarkovTransition"
    
    @property
    def paper_id(self) -> str:
        return "2601.04959"
    
    @property
    def description(self) -> str:
        return "Expected next move condition on current state (Adaptive)"
    
    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        mid_prices = (data["bid_prices"][:, 0] + data["ask_prices"][:, 0]) / 2.0
        returns = np.diff(mid_prices, prepend=mid_prices[0])
        states = np.sign(returns).astype(int) # -1, 0, 1
        
        # Adaptive Expectations for each state
        # E_up: Expected return given we just went UP
        # E_dn: Expected return given we just went DOWN
        # E_flat: Expected return given we were FLAT
        
        # Vectorized implementation using Numba would be best, but here we use loop or mask
        # Pure python loop is too slow? Length 100k. 
        # Numba is not available in this env (maybe). 
        # We can use numpy mask accumulation.
        
        n = len(states)
        expectations = np.zeros_(n) # The signal
        
        # Fast approximation: Global expanding window? 
        # No, needs to be rolling or EWMA to adapt.
        # Let's use a simplified logical structure.
        
        # We need to construct 3 series: NextRet where State=1, NextRet where State=-1, etc.
        # And EWMA smooth them.
        
        # Next Return (Target)
        target = np.roll(returns, -1) 
        target[-1] = 0
        
        # Masks
        is_up = (states == 1)
        is_dn = (states == -1)
        is_flat = (states == 0)
        
        # Fill series to smooth
        # For 'up_series', we want values of 'target' where is_up, and persist previous estimates otherwise?
        # Actually, standard EWMA update: E_new = E_old + alpha * (Target - E_old) only if State matches.
        # conditional_ewma(target, mask, span=100)
        
        def conditional_ewma_loop(target_arr, mask_arr, span=100):
            out = np.zeros_like(target_arr)
            alpha = 2 / (span + 1)
            curr = 0.0
            for i in range(len(target_arr)):
                if mask_arr[i]:
                    curr = alpha * target_arr[i] + (1 - alpha) * curr
                out[i] = curr
            return out
            
        # Since we can't use complex numba, we'll try to stick to numpy or looping if needed.
        # Python loop for 100k IS acceptable if operations are simple float. 
        # Let's try to optimize: 
        # We can simply index the 'up' occurrences, ewma them, and scatter back?
        # Yes! 
        
        def vectorized_conditional_ewma(targets, mask, span=100):
            # 1. Extract values where mask is True
            valid_targets = targets[mask]
            if len(valid_targets) == 0:
                return np.zeros_like(targets)
                
            # 2. EWMA on valid sequence
            # Pandas ewma is fast.
            # Convert to pandas? Backtester has pandas.
            # Assuming we can use simple recursion or just average. 
            # Let's use 'scipy.signal.lfilter' for EWMA? 
            # Or simple Python loop on the shorter sequence.
            
            # Simple Python loop on subset is fast.
            vals = np.zeros_like(valid_targets)
            curr = 0.0
            alpha = 2 / (span + 1)
            for i in range(len(valid_targets)):
                curr = curr * (1 - alpha) + valid_targets[i] * alpha
                vals[i] = curr
                
            # 3. Scatter back. We need to fill-forward values for non-mask steps (persistence)
            full_vals = np.zeros_like(targets)
            full_vals[mask] = vals
            
            # Fill gaps? The logic is: "What is my expectation NOW?"
            # If state is UP, my expectation is E_up.
            # I don't need E_dn. 
            # So I only need the generated value AT the mask.
            # BUT, the signal at time t depends on State(t).
            # So Signal[t] = E[Next | State(t)].
            # E[Next | State(t)] is the current value of the EWMA corresponding to State(t).
            # So we DO need the 'latest' estimate.
            
            # Scatter gives estimates at update points.
            # We then Forward Fill to propagate the estimate.
            
            # Forward fill efficient in numpy:
            # mask indices
            idx = np.arange(len(targets))
            valid_idx = idx[mask]
            # We can use np.maximum.accumulate logic or similar but easier to just use loop logic in 1st pass.
            return full_vals, valid_idx

        # Actually, simpler:
        # We iterate t. 
        # State = S_t.
        # Signal = Estimates[S_t].
        # Observe R_{t+1}.
        # Update Estimates[S_t].
        
        # This couples the steps. Must loop?
        # 100k iters in python is < 0.5s. Acceptable.
        
        est_up = 0.0
        est_dn = 0.0
        est_flat = 0.0
        alpha = 0.02 # Window ~ 100
        
        signal = np.zeros(n)
        
        # To avoid Lookahead:
        # Signal[t] uses Est[State[t]] BEFORE observing Ret[t+1] (which is used to update).
        # But wait, we are updating with 'target' which is Ret[t+1].
        # So we create signal, THEN update.
        
        # Precompute target
        targets = np.roll(returns, -1)
        targets[-1] = 0.0
        
        # Loop
        for i in range(n - 1):
            s = states[i]
            
            if s == 1:
                signal[i] = est_up
                est_up = est_up * (1 - alpha) + targets[i] * alpha
            elif s == -1:
                signal[i] = est_dn
                est_dn = est_dn * (1 - alpha) + targets[i] * alpha
            else:
                signal[i] = est_flat
                est_flat = est_flat * (1 - alpha) + targets[i] * alpha
                
        return signal


class LiquidityResistanceFactor(FactorBase):
    """
    Liquidity Resistance Ratio
    Paper: 2601.03215 (Concept of Market Resistance)
    Logic: (BidDepth / BuyVol) - (AskDepth / SellVol)
    Measures 'Time to consume liquidity'. High Value -> Bullish (Hard to push down).
    """
    
    @property
    def name(self) -> str:
        return "LiquidityResistance"
    
    @property
    def paper_id(self) -> str:
        return "2601.03215"
    
    @property
    def description(self) -> str:
        return "Depth normalized by trade volume (Resistance Time)"
    
    def compute(self, data: Dict[str, np.ndarray]) -> np.ndarray:
        # Depth: Sum size at level 1? Or total depth?
        # Paper suggests resistance of the book. Let's use Top 5 levels sum.
        bid_v = np.sum(data["bid_volumes"], axis=1)
        ask_v = np.sum(data["ask_volumes"], axis=1)
        
        trade_vol = data["trade_volume"]
        trade_side = data["trade_side"]
        
        buy_vol = np.where(trade_side > 0, trade_vol, 0)
        sell_vol = np.where(trade_side < 0, trade_vol, 0)
        
        # Rolling Volume (Turnover)
        window = 100
        kernel = np.ones(window)
        # Add epsilon to avoid div by zero
        roll_buy = np.convolve(buy_vol, kernel, mode='same') + 1.0
        roll_sell = np.convolve(sell_vol, kernel, mode='same') + 1.0
        
        # Resistance = Depth / Rate
        res_bid = bid_v / roll_sell # Selling eats Bid liquidity
        res_ask = ask_v / roll_buy  # Buying eats Ask liquidity
        
        return res_bid - res_ask
