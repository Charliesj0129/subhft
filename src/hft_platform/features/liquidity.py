from collections import deque
import math

class ShadowQueueEstimator:
    """
    Estimates the position in the queue for a passive order placed NOW.
    Real queue position requires tracking order-id, but shadow queue 
    estimates 'how much volume needs to be eaten' before a theoretical order at this price is filled.
    """
    def __init__(self):
        pass
        
    def estimate(self, lob: dict, side: str, level: int = 0) -> int:
        """
        Returns volume ahead in queue.
        If level=0 (Best Bid/Ask), it returns the current volume.
        This represents the queue size one would join at the END of.
        """
        # If we join NOW, we are at the end.
        # So queue position = current total volume at that level.
        
        if side.lower() == 'buy':
            rows = lob.get("bids", [])
        else:
            rows = lob.get("asks", [])
            
        if len(rows) <= level:
            return 0
            
        # Volume at Price Level
        return rows[level][1]

class RollingKyleLambda:
    """
    Estimates Kyle's Lambda (Price Impact Cost).
    Delta P = Lambda * NetFlow + Noise
    
    We use Rolling OLS (Ordinary Least Squares).
    Lambda = Cov(dP, Flow) / Var(Flow)
    """
    def __init__(self, window_size: int = 100):
        self.window_size = window_size
        self.dp_buffer = deque(maxlen=window_size)
        self.flow_buffer = deque(maxlen=window_size)
        
        # Incremental stats could be used, but recalculating on deque is robust for small N
        
    def update(self, price_change: float, net_flow: float) -> float:
        """
        Updates the estimator and returns current Lambda.
        
        Args:
            price_change: Delta Mid Price
            net_flow: OFI or Signed Trade Volume
        
        Returns:
            float: Estimated Lambda (Slope).
        """
        self.dp_buffer.append(price_change)
        self.flow_buffer.append(net_flow)
        
        if len(self.dp_buffer) < 10:
            return 0.0
            
        # Calculate OLS Slope
        # Slope = Sum((x - x_bar)(y - y_bar)) / Sum((x - x_bar)^2)
        # x = flow, y = dp
        
        flow_mean = sum(self.flow_buffer) / len(self.flow_buffer)
        dp_mean = sum(self.dp_buffer) / len(self.dp_buffer)
        
        numerator = 0.0
        denominator = 0.0
        
        # Optimization: numpy is faster but avoiding deps for core HFT loop if possible
        # Pure python is fine for window=100
        for f, dp in zip(self.flow_buffer, self.dp_buffer):
            f_diff = f - flow_mean
            numerator += f_diff * (dp - dp_mean)
            denominator += f_diff * f_diff
            
        if denominator == 0:
            return 0.0
            
        return numerator / denominator
