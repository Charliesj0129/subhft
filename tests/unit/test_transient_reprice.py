import os
import sys

import numpy as np
import pytest

sys.path.append(os.path.abspath("research/tools"))
sys.path.append(os.path.abspath("research/tools/legacy"))

factor_registry = pytest.importorskip("factor_registry", reason="factor registry not available")
TransientRepriceFactor = factor_registry.TransientRepriceFactor
alpha_backtester_mod = pytest.importorskip("alpha_backtester", reason="alpha_backtester not available")
AlphaBacktester = alpha_backtester_mod.AlphaBacktester


class TestTransientReprice:
    def test_zigzag_reversion(self):
        """
        Verify that TransientReprice correctly predicts reversals in a perfect zigzag market.
        Price: 100 -> 110 -> 100 -> 110 ...
        """
        n = 1000
        mid = np.zeros(n)

        for i in range(n):
            cycle = i % 20
            if cycle <= 10:
                mid[i] = 100 + cycle
            else:
                mid[i] = 110 - (cycle - 10)

        data = {
            "bid_prices": mid.reshape(-1, 1),
            "ask_prices": mid.reshape(-1, 1),
            "bid_volumes": np.ones((n, 1)),
            "ask_volumes": np.ones((n, 1)),
        }

        factor = TransientRepriceFactor()
        signal = factor.compute(data)

        valid_idx = 10
        assert np.isfinite(signal[valid_idx]).all()

        backtester = AlphaBacktester(horizon=10)
        from factor_registry import FactorResult

        f_res = FactorResult(signal, "TransientReprice", "Test", "Desc")
        res = backtester.run_single(data, f_res)

        print(f"\nResult: IC={res.ic:.4f}, Sharpe={res.sharpe:.2f}")

        assert res.ic > 0.8, f"Expected high positive IC for perfect reversion, got {res.ic}"
        assert res.sharpe > 5.0


if __name__ == "__main__":
    t = TestTransientReprice()
    t.test_zigzag_reversion()
