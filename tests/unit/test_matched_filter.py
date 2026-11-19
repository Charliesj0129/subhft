import numpy as np
import pytest

factor_registry = pytest.importorskip(
    "hft_platform.research.tools.factor_registry", reason="research tools not available"
)
MatchedFilterTradeFlowFactor = factor_registry.MatchedFilterTradeFlowFactor
_compute_matched_filter_flow = factor_registry._compute_matched_filter_flow


def test_matched_filter_logic():
    """Test that signal uses long-term volume for normalization"""
    n = 1000
    trade_vol = np.ones(n) * 100.0
    trade_side = np.ones(n)

    trade_vol[500] = 1000.0

    fast_w = 10
    slow_w = 100

    signal = _compute_matched_filter_flow(trade_vol, trade_side, fast_w, slow_w)

    assert np.isclose(signal[200], 10.0)

    val = signal[500]
    expected = (9 * 100 + 1000) / ((99 * 100 + 1000) / 100)
    assert np.isclose(val, expected)


def test_factor_class():
    factor = MatchedFilterTradeFlowFactor(window_size=10, slow_window_ratio=10)
    assert factor.slow_window == 100
    assert factor.name == "MatchedFilterTradeFlow"
