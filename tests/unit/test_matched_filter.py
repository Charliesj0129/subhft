
import numpy as np
import pytest

factor_registry = pytest.importorskip(
    "hft_platform.research.tools.factor_registry", reason="research tools not available"
)
MatchedFilterTradeFlowFactor = factor_registry.MatchedFilterTradeFlowFactor
_compute_matched_filter_flow = factor_registry._compute_matched_filter_flow

def test_matched_filter_logic():
    """Test that signal uses long-term volume for normalization"""
    # Create synthetic data
    n = 1000
    trade_vol = np.ones(n) * 100.0  # Constant volume
    trade_side = np.ones(n)         # All buys

    # Introduce a spike in volume at t=500
    trade_vol[500] = 1000.0

    # Fast window = 10, Slow window = 100
    fast_w = 10
    slow_w = 100

    signal = _compute_matched_filter_flow(trade_vol, trade_side, fast_w, slow_w)

    # Check steady state
    # At t=200, avg vol (slow) = 100. sum signed (fast) = 10 * 100 = 1000.
    # Signal = 1000 / 100 = 10.
    assert np.isclose(signal[200], 10.0)

    # Check spike impact
    # At t=500, fast sum includes spike?
    # fast sum at 500 (inclusive) -> 9*100 + 1000 = 1900
    # slow mean at 500 (inclusive) -> (99*100 + 1000)/100 = 10900/100 = 109
    # Signal = 1900 / 109 ≈ 17.43

    # Compare with "Standard" normalization (Trade Imbalance usually / Volume)
    # Standard would be: NetFlow / TotalVolume over same window.
    # Standard Signal at 500: 1900 / 1900 = 1.0 (assuming Volume weighted) or similar.
    # Matched Filter preserves the MAGNITUDE of the spike because denominator (109) < numerator (1900).

    val = signal[500]
    expected = (9*100 + 1000) / ((99*100 + 1000)/100)
    assert np.isclose(val, expected)

def test_factor_class():
    factor = MatchedFilterTradeFlowFactor(window_size=10, slow_window_ratio=10)
    assert factor.slow_window == 100
    assert factor.name == "MatchedFilterTradeFlow"
