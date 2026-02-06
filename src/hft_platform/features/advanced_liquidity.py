import math
from collections import deque

import numpy as np


class RollingEffectiveSpread:
    """
    Estimates Effective Spread using Roll's Model (1984).
    Spread = 2 * sqrt(-Cov(Delta P_t, Delta P_t-1))

    If Covariance is positive (trending), Roll Spread is undefined (or 0).
    """

    def __init__(self, window_size: int = 100):
        self.window_size = window_size
        self.prices: deque[float] = deque(maxlen=window_size + 1)

    def update(self, price: float) -> float:
        """
        Update with new price and return estimated spread.
        """
        self.prices.append(price)

        if len(self.prices) < 10:
            return 0.0

        # Compute Deltas
        p_arr = np.array(self.prices)
        deltas = np.diff(p_arr)  # Length N-1

        # We need Cov(d_t, d_t-1)
        # d_t: deltas[1:]
        # d_t-1: deltas[:-1]

        d_t = deltas[1:]
        d_prev = deltas[:-1]

        if len(d_t) < 5:
            return 0.0

        # Covariance matrix [ [var(x), cov(x,y)], [cov(y,x), var(y)] ]
        cov_matrix = np.cov(d_t, d_prev)
        autocov = cov_matrix[0, 1]

        if autocov >= 0:
            return 0.0

        return 2.0 * math.sqrt(-autocov)


class RollingAmihud:
    """
    Estimates Amihud Illiquidity Ratio (Impact per Dollar).
    ILLIQ = Mean( |Ret| / (Price * Vol) )
    """

    def __init__(self, window_size: int = 100):
        self.window_size = window_size
        self.ratios: deque[float] = deque(maxlen=window_size)

    def update(self, return_val: float, price: float, volume: float) -> float:
        """
        Args:
            return_val: Percentage or Log return.
            price: Current price.
            volume: Traded volume.
        """
        if volume == 0 or price == 0:
            ratio = 0.0
        else:
            ratio = abs(return_val) / (price * volume)

        self.ratios.append(ratio)

        if len(self.ratios) < 5:
            return 0.0

        return float(np.mean(self.ratios))
