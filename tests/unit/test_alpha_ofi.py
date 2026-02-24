"""Unit tests for alpha_ofi strategy module (T2 coverage)."""
from __future__ import annotations

import pytest

numba = pytest.importorskip("numba", reason="numba required for alpha_ofi")


def test_module_imports():
    from hft_platform.strategies.alpha import alpha_ofi  # noqa: F401

    assert hasattr(alpha_ofi, "strategy")
    assert callable(alpha_ofi.strategy)


def test_alpha_ofi_initial_state():
    """AlphaOFI should start with NaN bid/ask and zero OFI."""
    from hft_platform.strategies.alpha.alpha_ofi import AlphaOFI
    import numpy as np

    alpha = AlphaOFI()
    assert np.isnan(alpha.prev_bid_p)
    assert np.isnan(alpha.prev_ask_p)
    assert alpha.ofi == pytest.approx(0.0)
    assert alpha.obi == pytest.approx(0.0)


def test_alpha_ofi_first_update_initializes():
    """First update should initialize prev prices without computing OFI."""
    from hft_platform.strategies.alpha.alpha_ofi import AlphaOFI
    import numpy as np

    alpha = AlphaOFI()
    # jitclass methods require positional arguments
    alpha.update(100.0, 101.0, 10.0, 8.0)
    assert alpha.prev_bid_p == pytest.approx(100.0)
    assert alpha.prev_ask_p == pytest.approx(101.0)
    # OFI not computed on first call
    assert alpha.ofi == pytest.approx(0.0)


def test_alpha_ofi_bid_price_increase():
    """Bid price increase → positive e_bid contribution to OFI."""
    from hft_platform.strategies.alpha.alpha_ofi import AlphaOFI

    alpha = AlphaOFI()
    # positional: bid_p, ask_p, bid_v, ask_v
    alpha.update(100.0, 101.0, 10.0, 8.0)
    # Bid price increases: e_bid = new bid_v = 12
    alpha.update(100.5, 101.0, 12.0, 8.0)
    # e_bid = 12 (price up), e_ask = 0 (same price, 8-8=0)
    assert alpha.ofi == pytest.approx(12.0)


def test_alpha_ofi_obi_range():
    """Order Book Imbalance should be in [-1, 1]."""
    from hft_platform.strategies.alpha.alpha_ofi import AlphaOFI

    alpha = AlphaOFI()
    alpha.update(100.0, 101.0, 5.0, 5.0)
    alpha.update(100.0, 101.0, 8.0, 2.0)
    assert -1.0 <= alpha.obi <= 1.0


def test_strategy_function_exists():
    from hft_platform.strategies.alpha.alpha_ofi import ofi_strategy

    assert callable(ofi_strategy)


