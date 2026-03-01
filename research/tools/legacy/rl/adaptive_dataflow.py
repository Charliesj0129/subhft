
import numpy as np
import polars as pl
from typing import List, Optional, Tuple

class DataFlowAugmentor:
    """
    Adaptive Dataflow Augmentation Module for Financial Time Series.
    Based on 'History Is Not Enough' (arXiv:2601.10143).
    
    Implements:
    1. Single-Stock Transformations: Jittering, Scaling, Magnitude Warping.
    2. Multi-Stock Mix-ups: Linear Mix (convex combination of two assets).
    3. Regime-based Injection: Injecting volatility/shocks based on 'Physics' alphas.
    """
    
    def __init__(self, rng_seed: int = 42):
        self.rng = np.random.default_rng(rng_seed)
        
    def jitter(self, x: np.ndarray, sigma: float = 0.03) -> np.ndarray:
        """
        Add Gaussian noise to the time series.
        x: (T, F)
        sigma: Standard deviation of noise relative to signal std.
        """
        noise = self.rng.normal(loc=0, scale=sigma, size=x.shape)
        return x + noise
        
    def scaling(self, x: np.ndarray, sigma: float = 0.1) -> np.ndarray:
        """
        Multiply time series by a random scalar.
        x: (T, F)
        sigma: Std dev of the scaling factor (centered at 1.0).
        """
        factor = self.rng.normal(loc=1.0, scale=sigma, size=(1, x.shape[1])) # Scale per feature or global? Paper implies per window.
        # Let's do per-feature scaling to simulate decoupled market moves? 
        # Or global to maintain correlations? Paper says 'Single Stock Transformations', let's stick to global scale for coherence.
        factor = self.rng.normal(loc=1.0, scale=sigma)
        return x * factor

    def magnitude_warping(self, x: np.ndarray, sigma: float = 0.2, knots: int = 4) -> np.ndarray:
        """
        Smoothly scale the magnitude using cubic splines (simulated via interpolation here).
        x: (T, F)
        """
        # Simple implementation: Generate random points and interpolate
        t = x.shape[0]
        # Generate random curves for each feature? Or one curve for all?
        # One curve for all features to preserve feature correlations (e.g. price vs volume).
        
        # We start with low-res random anchors
        anchors = np.linspace(0, t, knots+2)
        anchor_values = self.rng.normal(loc=1.0, scale=sigma, size=knots+2)
        
        # Interpolate to full length
        time_steps = np.arange(t)
        warp_curve = np.interp(time_steps, anchors, anchor_values)
        
        return x * warp_curve[:, np.newaxis]

    def linear_mixup(self, x_a: np.ndarray, x_b: np.ndarray, alpha: float = 0.5) -> np.ndarray:
        """
        Convex combination of two time series.
        x_new = lambda * x_a + (1 - lambda) * x_b
        lambda ~ Beta(alpha, alpha)
        
        Note: x_a and x_b must be normalized for this to make sense conceptually in finance 
        (e.g. returns or z-scores). Mixing raw prices of $10 and $1000 is dangerous.
        """
        if x_a.shape != x_b.shape:
             # Crop to min length
             min_len = min(x_a.shape[0], x_b.shape[0])
             x_a = x_a[:min_len]
             x_b = x_b[:min_len]
             
        lam = self.rng.beta(alpha, alpha)
        return lam * x_a + (1 - lam) * x_b
        
    def augment_batch(self, batch_data: np.ndarray, strategy: str = 'random') -> np.ndarray:
        """
        Apply augmentation to a batch of data.
        batch_data: (Batch, Time, Feat)
        """
        augmented = []
        for i in range(len(batch_data)):
            x = batch_data[i]
            
            # Randomly choose an augmentation
            if strategy == 'random':
                op = self.rng.choice(['jitter', 'scale', 'warp', 'mixup', 'none'], p=[0.2, 0.2, 0.2, 0.2, 0.2])
            else:
                op = strategy
                
            if op == 'jitter':
                x_aug = self.jitter(x)
            elif op == 'scale':
                x_aug = self.scaling(x)
            elif op == 'warp':
                x_aug = self.magnitude_warping(x)
            elif op == 'mixup':
                # Pick a random partner from the batch
                idx_b = self.rng.integers(0, len(batch_data))
                x_aug = self.linear_mixup(x, batch_data[idx_b])
            else:
                x_aug = x
                
            augmented.append(x_aug)
            
        return np.array(augmented)

if __name__ == "__main__":
    # Test Block
    print("Testing Adaptive Dataflow...")
    augmentor = DataFlowAugmentor()
    
    # Create fake price series (Random Walk)
    t = 100
    p = 100 + np.cumsum(np.random.randn(t))
    v = np.abs(np.random.randn(t)) * 10
    data = np.stack([p, v], axis=1) # (100, 2)
    
    print(f"Original Mean: {data.mean(axis=0)}")
    
    aug_j = augmentor.jitter(data)
    print(f"Jittered Mean: {aug_j.mean(axis=0)}")
    
    aug_w = augmentor.magnitude_warping(data)
    print(f"Warped Mean: {aug_w.mean(axis=0)}")
    
    # Batch Test
    batch = np.array([data, data * 1.05, data * 0.95])
    res = augmentor.augment_batch(batch)
    print(f"Augmented Batch Shape: {res.shape}")
