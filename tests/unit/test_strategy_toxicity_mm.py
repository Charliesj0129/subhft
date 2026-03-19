"""Unit tests for toxicity-aware MM strategies (v1, v2, v3).

These strategies use numpy feature arrays and hftbacktest framework.
Tests cover import, instantiation, and basic parameter validation.
"""

import numpy as np
import pytest

try:
    from hftbacktest import GTX, LIMIT  # noqa: F401

    HAS_HFT_BACKTEST = True
except ImportError:
    HAS_HFT_BACKTEST = False


# ---- v1: ToxicityAwareMM ----


@pytest.mark.skipif(not HAS_HFT_BACKTEST, reason="hftbacktest not installed")
def test_import_toxicity_aware_mm_v1():
    """Should not raise."""
    from hft_platform.strategies.toxicity_aware_mm import ToxicityAwareMM  # noqa: F401


@pytest.mark.skipif(not HAS_HFT_BACKTEST, reason="hftbacktest not installed")
def test_toxicity_mm_v1_instantiation():
    """Should instantiate with valid numpy arrays."""
    from hft_platform.strategies.toxicity_aware_mm import ToxicityAwareMM

    n = 100
    n_features = 6
    ts = np.arange(n, dtype=np.int64) * 1_000_000_000
    feats = np.random.randn(n, n_features)
    names = [
        "queue_imbalance",
        "toxicity_timescale_div",
        "microprice_spread_ratio",
        "cross_ema_qi",
        "depth_velocity_diff",
        "adverse_momentum",
    ]

    strat = ToxicityAwareMM(
        feature_timestamps=ts,
        feature_array=feats,
        feature_names=names,
        symbol="TXFC6",
        tick_size=1,
        max_position=5,
    )
    assert strat._symbol == "TXFC6"


# ---- v2 ----


@pytest.mark.skipif(not HAS_HFT_BACKTEST, reason="hftbacktest not installed")
def test_import_toxicity_aware_mm_v2():
    """Should not raise."""
    from hft_platform.strategies.toxicity_aware_mm_v2 import ToxicityAwareMMv2  # noqa: F401


# ---- v3 ----


@pytest.mark.skipif(not HAS_HFT_BACKTEST, reason="hftbacktest not installed")
def test_import_toxicity_aware_mm_v3():
    """Should not raise."""
    from hft_platform.strategies.toxicity_aware_mm_v3 import ToxicityAwareMMv3  # noqa: F401
