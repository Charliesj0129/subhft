"""Tests for crash recovery position merge into PositionStore.

Verifies that recovery positions (loaded via load_recovery) are correctly
merged on the first live fill, so PnL is calculated against the recovered
avg_price rather than zero.

All prices/PnL values use scaled integers (x10000).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from hft_platform.contracts.execution import FillEvent, Side
from hft_platform.execution.positions import PositionStore

SCALE = 10_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fill(
    symbol: str = "2330",
    side: Side = Side.SELL,
    qty: int = 1,
    price_scaled: int = 110 * SCALE,
    account_id: str = "acc1",
    strategy_id: str = "strat_a",
    fee: int = 0,
    tax: int = 0,
) -> FillEvent:
    return FillEvent(
        fill_id="f001",
        account_id=account_id,
        order_id="o001",
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price_scaled,
        fee=fee,
        tax=tax,
        ingest_ts_ns=1_000_000,
        match_ts_ns=1_000_000,
    )


def _make_store_no_rust() -> PositionStore:
    """Create a PositionStore with Rust tracker disabled for Python-path tests."""
    with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0"}):
        store = PositionStore()
    store._rust_tracker = None
    store.metadata = MagicMock()
    store.metadata.contract_multiplier.return_value = 1
    store.metrics = None
    return store


class _MockRustTracker:
    """Minimal mock of RustPositionTracker that mirrors Python Position.update logic."""

    def __init__(self) -> None:
        self._positions: dict[str, list] = {}  # key -> [net_qty, avg_price, rpnl, fees]

    def update(
        self,
        key: str,
        side: int,
        qty: int,
        price: int,
        fee: int,
        tax: int,
        ts: int,
        multiplier: int,
    ) -> tuple[int, int, int, int]:
        state = self._positions.get(key)
        if state is None:
            state = [0, 0, 0, 0]
            self._positions[key] = state

        net_qty, avg_price, rpnl, fees = state
        is_buy = side == 0
        signed = qty if is_buy else -qty

        fees += fee + tax

        current_sign = 1 if net_qty > 0 else (-1 if net_qty < 0 else 0)
        fill_sign = 1 if is_buy else -1
        closing = current_sign != 0 and fill_sign != current_sign

        if closing:
            close_qty = min(abs(net_qty), qty)
            if is_buy:
                pnl = (avg_price - price) * close_qty * multiplier
            else:
                pnl = (price - avg_price) * close_qty * multiplier
            rpnl += pnl
            net_qty += signed
            if net_qty == 0:
                avg_price = 0
            elif (current_sign > 0 and net_qty < 0) or (current_sign < 0 and net_qty > 0):
                avg_price = price
        else:
            if net_qty == 0:
                avg_price = price
                net_qty += signed
            else:
                total_val = net_qty * avg_price + signed * price
                net_qty += signed
                if net_qty != 0:
                    avg_price = (2 * total_val + net_qty) // (2 * net_qty)

        state[:] = [net_qty, avg_price, rpnl, fees]
        return (net_qty, avg_price, rpnl, fees)

    def get(self, key: str) -> tuple[int, int, int, int] | None:
        state = self._positions.get(key)
        if state is None:
            return None
        return tuple(state)  # type: ignore[return-value]


def _make_store_with_mock_rust() -> PositionStore:
    """Create a PositionStore with a mock Rust tracker."""
    with patch.dict(os.environ, {"HFT_RUST_POSITIONS": "0"}):
        store = PositionStore()
    store._rust_tracker = _MockRustTracker()  # type: ignore[assignment]
    store.metadata = MagicMock()
    store.metadata.contract_multiplier.return_value = 1
    store.metrics = None
    return store


# ---------------------------------------------------------------------------
# Tests: Python path
# ---------------------------------------------------------------------------


class TestRecoveryMergePythonPath:
    """Recovery merge via the Python position update path."""

    def test_closing_fill_uses_recovered_avg_price(self):
        """Load recovery long 10@100, sell 10@110 => PnL = (110-100)*10 * SCALE."""
        store = _make_store_no_rust()
        store.load_recovery(
            account_id="acc1",
            symbol="2330",
            net_qty=10,
            avg_price_scaled=100 * SCALE,
        )

        fill = _make_fill(
            symbol="2330",
            side=Side.SELL,
            qty=10,
            price_scaled=110 * SCALE,
            account_id="acc1",
            strategy_id="strat_a",
        )
        delta = store.on_fill(fill)

        # PnL = (110 - 100) * 10 * SCALE = 1_000_000
        assert delta.realized_pnl == 10 * 10 * SCALE
        assert delta.net_qty == 0
        # Position should exist under the strategy key
        key = "acc1:strat_a:2330"
        assert key in store.positions
        assert store.positions[key].realized_pnl_scaled == 10 * 10 * SCALE

    def test_recovery_includes_historical_rpnl(self):
        """Recovery with existing realized_pnl_scaled=5000 is preserved."""
        store = _make_store_no_rust()
        historical_rpnl = 5000
        store.load_recovery(
            account_id="acc1",
            symbol="2330",
            net_qty=5,
            avg_price_scaled=100 * SCALE,
            realized_pnl_scaled=historical_rpnl,
        )

        fill = _make_fill(
            symbol="2330",
            side=Side.SELL,
            qty=5,
            price_scaled=110 * SCALE,
            account_id="acc1",
            strategy_id="strat_a",
        )
        delta = store.on_fill(fill)

        # New PnL from this fill = (110-100)*5*SCALE = 500_000
        new_pnl = 10 * 5 * SCALE
        assert delta.realized_pnl == historical_rpnl + new_pnl
        assert store.positions["acc1:strat_a:2330"].realized_pnl_scaled == historical_rpnl + new_pnl

    def test_recovery_includes_historical_fees(self):
        """Recovery with existing fees_scaled is preserved and accumulated."""
        store = _make_store_no_rust()
        store.load_recovery(
            account_id="acc1",
            symbol="2330",
            net_qty=5,
            avg_price_scaled=100 * SCALE,
            fees_scaled=2000,
        )

        fill = _make_fill(
            symbol="2330",
            side=Side.SELL,
            qty=5,
            price_scaled=110 * SCALE,
            account_id="acc1",
            strategy_id="strat_a",
            fee=100,
            tax=50,
        )
        store.on_fill(fill)

        pos = store.positions["acc1:strat_a:2330"]
        # Recovery fees + fill fees
        assert pos.fees_scaled == 2000 + 100 + 50

    def test_no_merge_when_no_recovery(self):
        """Normal fill without recovery data works as before (opens new position)."""
        store = _make_store_no_rust()

        fill = _make_fill(
            symbol="2330",
            side=Side.BUY,
            qty=10,
            price_scaled=100 * SCALE,
            account_id="acc1",
            strategy_id="strat_a",
        )
        delta = store.on_fill(fill)

        assert delta.net_qty == 10
        assert delta.realized_pnl == 0
        assert "acc1:strat_a:2330" in store.positions

    def test_no_stale_recovery_key_after_merge(self):
        """After merge, the recovery entry is consumed; no acc::sym key exists."""
        store = _make_store_no_rust()
        store.load_recovery(
            account_id="acc1",
            symbol="2330",
            net_qty=10,
            avg_price_scaled=100 * SCALE,
        )

        fill = _make_fill(
            symbol="2330",
            side=Side.SELL,
            qty=5,
            price_scaled=110 * SCALE,
            account_id="acc1",
            strategy_id="strat_a",
        )
        store.on_fill(fill)

        # Recovery entry consumed
        assert "acc1:2330" not in store._recovery_positions
        # No stale empty-strategy key
        assert "acc1::2330" not in store.positions
        # Only the proper strategy key
        assert "acc1:strat_a:2330" in store.positions

    def test_zero_qty_recovery_is_ignored(self):
        """load_recovery with net_qty=0 stores nothing."""
        store = _make_store_no_rust()
        store.load_recovery(
            account_id="acc1",
            symbol="2330",
            net_qty=0,
            avg_price_scaled=100 * SCALE,
        )
        assert len(store._recovery_positions) == 0

    def test_short_recovery_position_merge(self):
        """Recovery short -5@200, cover buy 5@190 => PnL = (200-190)*5*SCALE."""
        store = _make_store_no_rust()
        store.load_recovery(
            account_id="acc1",
            symbol="2330",
            net_qty=-5,
            avg_price_scaled=200 * SCALE,
        )

        fill = _make_fill(
            symbol="2330",
            side=Side.BUY,
            qty=5,
            price_scaled=190 * SCALE,
            account_id="acc1",
            strategy_id="strat_a",
        )
        delta = store.on_fill(fill)

        # PnL for covering short = (avg_price - fill_price) * qty = (200-190)*5*SCALE
        assert delta.realized_pnl == 10 * 5 * SCALE
        assert delta.net_qty == 0

    def test_second_fill_does_not_re_merge(self):
        """After first fill merges recovery, subsequent fills work normally."""
        store = _make_store_no_rust()
        store.load_recovery(
            account_id="acc1",
            symbol="2330",
            net_qty=10,
            avg_price_scaled=100 * SCALE,
        )

        # First fill: partial close
        fill1 = _make_fill(
            symbol="2330",
            side=Side.SELL,
            qty=5,
            price_scaled=110 * SCALE,
            account_id="acc1",
            strategy_id="strat_a",
        )
        store.on_fill(fill1)

        # Second fill: close remaining
        fill2 = _make_fill(
            symbol="2330",
            side=Side.SELL,
            qty=5,
            price_scaled=120 * SCALE,
            account_id="acc1",
            strategy_id="strat_a",
        )
        fill2.fill_id = "f002"
        delta2 = store.on_fill(fill2)

        # First close: (110-100)*5*SCALE = 500_000
        # Second close: (120-100)*5*SCALE = 1_000_000
        # Total = 1_500_000
        pos = store.positions["acc1:strat_a:2330"]
        assert pos.realized_pnl_scaled == 10 * 5 * SCALE + 20 * 5 * SCALE
        assert pos.net_qty == 0


# ---------------------------------------------------------------------------
# Tests: Rust (mock) path
# ---------------------------------------------------------------------------


class TestRecoveryMergeRustPath:
    """Recovery merge via the Rust position tracker path (mocked)."""

    def test_closing_fill_uses_recovered_avg_price_rust(self):
        """Rust path: recovery long 10@100, sell 10@110 => correct PnL."""
        store = _make_store_with_mock_rust()
        store.load_recovery(
            account_id="acc1",
            symbol="2330",
            net_qty=10,
            avg_price_scaled=100 * SCALE,
        )

        fill = _make_fill(
            symbol="2330",
            side=Side.SELL,
            qty=10,
            price_scaled=110 * SCALE,
            account_id="acc1",
            strategy_id="strat_a",
        )
        delta = store.on_fill(fill)

        assert delta.realized_pnl == 10 * 10 * SCALE
        assert delta.net_qty == 0

    def test_rust_path_preserves_historical_rpnl(self):
        """Rust path: historical rpnl from recovery is added to Rust-computed PnL."""
        store = _make_store_with_mock_rust()
        historical_rpnl = 7777
        store.load_recovery(
            account_id="acc1",
            symbol="2330",
            net_qty=5,
            avg_price_scaled=100 * SCALE,
            realized_pnl_scaled=historical_rpnl,
        )

        fill = _make_fill(
            symbol="2330",
            side=Side.SELL,
            qty=5,
            price_scaled=110 * SCALE,
            account_id="acc1",
            strategy_id="strat_a",
        )
        delta = store.on_fill(fill)

        new_pnl = 10 * 5 * SCALE
        assert delta.realized_pnl == historical_rpnl + new_pnl
        pos = store.positions["acc1:strat_a:2330"]
        assert pos.realized_pnl_scaled == historical_rpnl + new_pnl

    def test_rust_path_preserves_historical_fees(self):
        """Rust path: historical fees from recovery are added to Rust-computed fees."""
        store = _make_store_with_mock_rust()
        store.load_recovery(
            account_id="acc1",
            symbol="2330",
            net_qty=5,
            avg_price_scaled=100 * SCALE,
            fees_scaled=3000,
        )

        fill = _make_fill(
            symbol="2330",
            side=Side.SELL,
            qty=5,
            price_scaled=110 * SCALE,
            account_id="acc1",
            strategy_id="strat_a",
            fee=200,
            tax=100,
        )
        store.on_fill(fill)

        pos = store.positions["acc1:strat_a:2330"]
        assert pos.fees_scaled == 3000 + 200 + 100

    def test_rust_path_second_fill_still_has_offset(self):
        """Rust path: offset persists across multiple fills for the same key."""
        store = _make_store_with_mock_rust()
        historical_rpnl = 5000
        store.load_recovery(
            account_id="acc1",
            symbol="2330",
            net_qty=10,
            avg_price_scaled=100 * SCALE,
            realized_pnl_scaled=historical_rpnl,
        )

        # First fill: partial close
        fill1 = _make_fill(
            symbol="2330",
            side=Side.SELL,
            qty=5,
            price_scaled=110 * SCALE,
            account_id="acc1",
            strategy_id="strat_a",
        )
        store.on_fill(fill1)

        # Second fill: close remaining
        fill2 = _make_fill(
            symbol="2330",
            side=Side.SELL,
            qty=5,
            price_scaled=120 * SCALE,
            account_id="acc1",
            strategy_id="strat_a",
        )
        fill2.fill_id = "f002"
        store.on_fill(fill2)

        pos = store.positions["acc1:strat_a:2330"]
        # Rust computes: first close (110-100)*5*SCALE + second close (120-100)*5*SCALE
        rust_rpnl = 10 * 5 * SCALE + 20 * 5 * SCALE
        assert pos.realized_pnl_scaled == historical_rpnl + rust_rpnl


# ---------------------------------------------------------------------------
# Tests: startup_recon integration
# ---------------------------------------------------------------------------


class TestStartupReconWriteToStore:
    """Verify startup_recon._write_to_store uses load_recovery."""

    def test_write_to_store_uses_load_recovery(self):
        """_write_to_store should populate _recovery_positions, not positions directly."""
        store = _make_store_no_rust()

        from hft_platform.execution.startup_recon import StartupPositionVerifier

        verifier = StartupPositionVerifier(
            client=MagicMock(),
            position_store=store,
        )

        positions = {
            "2330": {"net_qty": 10, "avg_price_scaled": 100 * SCALE, "realized_pnl_scaled": 5000},
            "2317": {"net_qty": -5, "avg_price_scaled": 200 * SCALE},
        }
        count = verifier._write_to_store(positions, "acc1")

        assert count == 2
        # Should be in _recovery_positions, NOT in positions directly
        assert "acc1:2330" in store._recovery_positions
        assert "acc1:2317" in store._recovery_positions
        assert len(store.positions) == 0  # nothing written directly

    def test_write_to_store_zero_qty_skipped(self):
        """Zero-qty positions are skipped by load_recovery."""
        store = _make_store_no_rust()

        from hft_platform.execution.startup_recon import StartupPositionVerifier

        verifier = StartupPositionVerifier(
            client=MagicMock(),
            position_store=store,
        )

        positions = {
            "2330": {"net_qty": 0, "avg_price_scaled": 100 * SCALE},
        }
        count = verifier._write_to_store(positions, "acc1")

        assert count == 1  # _write_to_store counts iterations, not stored
        assert len(store._recovery_positions) == 0
