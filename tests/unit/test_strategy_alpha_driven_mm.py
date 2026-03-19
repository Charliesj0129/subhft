"""Unit tests for AlphaDrivenMMStrategy base strategy."""

import pytest
import numpy as np

try:
    from hftbacktest import GTX, LIMIT  # noqa: F401

    HAS_HFT_BACKTEST = True
except ImportError:
    HAS_HFT_BACKTEST = False


@pytest.mark.skipif(not HAS_HFT_BACKTEST, reason="hftbacktest not installed")
def test_import_alpha_driven_mm():
    """Should not raise."""
    from hft_platform.strategies.alpha_driven_mm import AlphaDrivenMMStrategy  # noqa: F401


@pytest.mark.skipif(not HAS_HFT_BACKTEST, reason="hftbacktest not installed")
def test_alpha_driven_mm_is_abstract():
    """AlphaDrivenMMStrategy requires compute_quotes to be implemented."""
    from hft_platform.strategies.alpha_driven_mm import AlphaDrivenMMStrategy

    n = 50
    n_features = 3
    ts = np.arange(n, dtype=np.int64) * 1_000_000_000
    feats = np.random.randn(n, n_features)
    names = ["feat_a", "feat_b", "feat_c"]

    # AlphaDrivenMMStrategy is abstract — cannot instantiate directly
    with pytest.raises(TypeError):
        AlphaDrivenMMStrategy(
            feature_timestamps=ts,
            feature_array=feats,
            feature_names=names,
            symbol="2330",
        )
