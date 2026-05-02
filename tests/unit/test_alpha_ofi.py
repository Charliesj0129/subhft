"""Unit tests for alpha_ofi strategy module (T2 coverage)."""

from __future__ import annotations

import os

import pytest

numba = pytest.importorskip("numba", reason="numba required for alpha_ofi")


def test_module_imports():
    from hft_platform.strategies.alpha import alpha_ofi  # noqa: F401

    assert hasattr(alpha_ofi, "strategy")
    assert callable(alpha_ofi.strategy)


def test_alpha_ofi_initial_state():
    """AlphaOFI should start with NaN bid/ask and zero OFI."""
    import numpy as np

    from hft_platform.strategies.alpha.alpha_ofi import AlphaOFI

    alpha = AlphaOFI()
    assert np.isnan(alpha.prev_bid_p)
    assert np.isnan(alpha.prev_ask_p)
    assert alpha.ofi == pytest.approx(0.0)
    assert alpha.obi == pytest.approx(0.0)


def test_alpha_ofi_first_update_initializes():
    """First update should initialize prev prices without computing OFI."""

    from hft_platform.strategies.alpha.alpha_ofi import AlphaOFI

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


def test_alpha_ofi_bid_price_decrease():
    """Bid price decrease → negative e_bid contribution (prev_bid_v removed)."""
    from hft_platform.strategies.alpha.alpha_ofi import AlphaOFI

    alpha = AlphaOFI()
    alpha.update(100.0, 101.0, 10.0, 8.0)
    # Bid price decreases: e_bid = -prev_bid_v = -10
    alpha.update(99.5, 101.0, 5.0, 8.0)
    # e_bid = -10 (price down), e_ask = 0 (same price)
    assert alpha.ofi == pytest.approx(-10.0)


def test_alpha_ofi_ask_price_decrease():
    """Ask price decrease → positive e_ask contribution to OFI (aggressive ask)."""
    from hft_platform.strategies.alpha.alpha_ofi import AlphaOFI

    alpha = AlphaOFI()
    alpha.update(100.0, 101.0, 10.0, 8.0)
    # Ask price decreases: e_ask = ask_v = 10 → OFI = e_bid - e_ask = 0 - 10 = -10
    alpha.update(100.0, 100.5, 10.0, 10.0)
    # e_bid = 0 (same price, 10-10=0), e_ask = 10 (ask price down → new qty)
    assert alpha.ofi == pytest.approx(-10.0)


def test_alpha_ofi_ask_price_increase():
    """Ask price increase → negative e_ask contribution (prev_ask_v removed)."""
    from hft_platform.strategies.alpha.alpha_ofi import AlphaOFI

    alpha = AlphaOFI()
    alpha.update(100.0, 101.0, 10.0, 8.0)
    # Ask price increases: e_ask = -prev_ask_v = -8 → OFI = e_bid - e_ask = 0 - (-8) = 8
    alpha.update(100.0, 102.0, 10.0, 5.0)
    # e_bid = 0 (same price, same qty), e_ask = -8 (ask price up → loses prev vol)
    assert alpha.ofi == pytest.approx(8.0)


def test_alpha_ofi_same_prices_volume_delta():
    """Same prices → OFI reflects volume change at bid and ask."""
    from hft_platform.strategies.alpha.alpha_ofi import AlphaOFI

    alpha = AlphaOFI()
    alpha.update(100.0, 101.0, 10.0, 8.0)
    # Same prices, bid vol up by 3, ask vol down by 2
    alpha.update(100.0, 101.0, 13.0, 6.0)
    # e_bid = 13-10 = 3, e_ask = 6-8 = -2 → OFI = 3 - (-2) = 5
    assert alpha.ofi == pytest.approx(5.0)


def test_alpha_ofi_zero_volume_obi():
    """Zero total volume should yield OBI = 0.0 (division by zero guard)."""
    from hft_platform.strategies.alpha.alpha_ofi import AlphaOFI

    alpha = AlphaOFI()
    alpha.update(100.0, 101.0, 10.0, 8.0)
    # Update with zero volumes
    alpha.update(100.0, 101.0, 0.0, 0.0)
    assert alpha.obi == pytest.approx(0.0)


def test_alpha_ofi_obi_fully_bid_side():
    """All volume on bid side should yield OBI close to 1.0."""
    from hft_platform.strategies.alpha.alpha_ofi import AlphaOFI

    alpha = AlphaOFI()
    alpha.update(100.0, 101.0, 0.0, 10.0)
    alpha.update(100.0, 101.0, 100.0, 0.0)
    assert alpha.obi == pytest.approx(1.0)


def test_alpha_ofi_obi_fully_ask_side():
    """All volume on ask side should yield OBI close to -1.0."""
    from hft_platform.strategies.alpha.alpha_ofi import AlphaOFI

    alpha = AlphaOFI()
    alpha.update(100.0, 101.0, 100.0, 0.0)
    alpha.update(100.0, 101.0, 0.0, 100.0)
    assert alpha.obi == pytest.approx(-1.0)


def test_alpha_ofi_state_persists_after_update():
    """After update, prev_* fields should hold the latest values."""
    from hft_platform.strategies.alpha.alpha_ofi import AlphaOFI

    alpha = AlphaOFI()
    alpha.update(100.0, 101.0, 10.0, 8.0)
    alpha.update(100.5, 101.5, 12.0, 9.0)
    assert alpha.prev_bid_p == pytest.approx(100.5)
    assert alpha.prev_ask_p == pytest.approx(101.5)
    assert alpha.prev_bid_v == pytest.approx(12.0)
    assert alpha.prev_ask_v == pytest.approx(9.0)


@pytest.mark.skipif(
    os.environ.get("NUMBA_DISABLE_JIT", "0") != "1",
    reason="strategy loop test requires NUMBA_DISABLE_JIT=1",
)
def test_ofi_strategy_runs_with_valid_depth():
    """ofi_strategy should process depth updates and return True."""
    from hft_platform.strategies.alpha.alpha_ofi import ofi_strategy

    class MockDepth:
        def __init__(self, bid: float, ask: float, bid_qty: float, ask_qty: float):
            self.best_bid = bid
            self.best_ask = ask
            self.best_bid_qty = bid_qty
            self.best_ask_qty = ask_qty

    class MockHbt:
        def __init__(self, ticks: int):
            self._ticks = ticks
            self._count = 0

        def elapse(self, _ns: int) -> int:
            if self._count >= self._ticks:
                return 1
            self._count += 1
            return 0

        def clear_inactive_orders(self, _asset_no: int) -> None:
            pass

        def depth(self, _asset_no: int) -> MockDepth:
            # Return valid bid/ask after first tick
            if self._count > 0:
                return MockDepth(100.0 + self._count * 0.1, 101.0 + self._count * 0.1, 10.0, 8.0)
            return MockDepth(0.0, 0.0, 0.0, 0.0)

    result = ofi_strategy(MockHbt(ticks=5))
    assert result is True


@pytest.mark.skipif(
    os.environ.get("NUMBA_DISABLE_JIT", "0") != "1",
    reason="strategy loop test requires NUMBA_DISABLE_JIT=1",
)
def test_ofi_strategy_skips_empty_depth():
    """ofi_strategy should skip (continue) when best_bid or best_ask is 0."""
    from hft_platform.strategies.alpha.alpha_ofi import ofi_strategy

    class EmptyDepth:
        best_bid = 0.0
        best_ask = 0.0
        best_bid_qty = 0.0
        best_ask_qty = 0.0

    class MockHbt:
        def __init__(self):
            self._count = 0

        def elapse(self, _ns: int) -> int:
            if self._count >= 3:
                return 1
            self._count += 1
            return 0

        def clear_inactive_orders(self, _asset_no: int) -> None:
            pass

        def depth(self, _asset_no: int):
            return EmptyDepth()

    result = ofi_strategy(MockHbt())
    assert result is True
