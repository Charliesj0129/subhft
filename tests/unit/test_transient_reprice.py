import os
import sys

import numpy as np
import pytest

sys.path.append(os.path.abspath("research/tools"))

factor_registry = pytest.importorskip("factor_registry", reason="factor registry not available")
TransientRepriceFactor = factor_registry.TransientRepriceFactor
from alpha_backtester import AlphaBacktester


class TestTransientReprice:
    def test_zigzag_reversion(self):
        """
        Verify that TransientReprice correctly predicts reversals in a perfect zigzag market.
        Price: 100 -> 110 -> 100 -> 110 ...
        """
        n = 1000
        mid = np.zeros(n)
        # Create zigzag with period 20 (10 up, 10 down) to match k=10 lag
        # But factor uses k=10.
        # If period is 20: 0..10 UP, 10..20 DOWN.
        # At t=10: Price=110, Prev=100. Ret=0.1. Signal = -0.1 (Predict DOWN).
        # Fwd(10): Price(20)=100. FwdRet = -0.09.
        # Signal (-0.1) matches FwdRet (-0.09) in sign. -> Positive IC.

        for i in range(n):
            cycle = i % 20
            if cycle <= 10:
                mid[i] = 100 + cycle # 100 -> 110
            else:
                mid[i] = 110 - (cycle - 10) # 110 -> 100

        data = {
            "bid_prices": mid.reshape(-1, 1),
            "ask_prices": mid.reshape(-1, 1), # mid = price
            "bid_volumes": np.ones((n, 1)),
            "ask_volumes": np.ones((n, 1)),
        }

        factor = TransientRepriceFactor()
        signal = factor.compute(data)

        # Check signal valid regions
        # k=10
        valid_idx = 10
        assert np.isfinite(signal[valid_idx]).all()

        # Run Backtest logic manually
        # Backtester horizon=10
        backtester = AlphaBacktester(horizon=10)
        from factor_registry import FactorResult

        f_res = FactorResult(signal, "TransientReprice", "Test", "Desc")
        res = backtester.run_single(data, f_res)

        print(f"\nResult: IC={res.ic:.4f}, Sharpe={res.sharpe:.2f}")

        # Should be strongly positive
        assert res.ic > 0.8, f"Expected high positive IC for perfect reversion, got {res.ic}"
        assert res.sharpe > 5.0

if __name__ == "__main__":
    t = TestTransientReprice()
    t.test_zigzag_reversion()
