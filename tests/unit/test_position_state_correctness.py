"""Regression tests for four position-state defects found 2026-08-23.

Every test in this file fails against the pre-fix tree. They cover the
boundary between the three places that each hold a piece of position state --
the Python dicts, the Rust tracker, and the on-disk checkpoint -- because each
defect was a disagreement between two of them rather than a fault inside one.
"""

import json
import threading

import pytest

from hft_platform.contracts.execution import FillEvent, Side
from hft_platform.execution.checkpoint import PositionCheckpointWriter
from hft_platform.execution.positions import PositionStore
from hft_platform.execution.startup_recon import MANUAL_STRATEGY_ID, StartupPositionVerifier


def _fill(symbol: str = "TXFE6", qty: int = 1, price: int = 1_800_000, *, strategy_id: str = "S1") -> FillEvent:
    return FillEvent(
        fill_id=f"F-{symbol}-{qty}-{price}",
        order_id="O-1",
        symbol=symbol,
        side=Side.BUY,
        qty=qty,
        price=price,
        fee=0,
        tax=0,
        ingest_ts_ns=1,
        match_ts_ns=1,
        strategy_id=strategy_id,
        account_id="ACC",
    )


class TestPortfolioAggregateIncludesRecovery:
    def test_recompute_does_not_discard_recovered_realized_pnl(self) -> None:
        """The O(n) recompute branch must count recovery positions.

        ``net_qty_for_symbol`` walks positions AND _recovery_positions; the
        aggregate recompute used to walk only the former, so the first opening
        fill after a crash restart zeroed the realized PnL recovery had just
        restored -- which can trip a false StormGuard drawdown HALT and is then
        written to disk by the next checkpoint.
        """
        store = PositionStore()
        store.load_recovery(
            account_id="ACC",
            symbol="TXFE6",
            net_qty=2,
            avg_price_scaled=1_800_000,
            realized_pnl_scaled=-50_000,
            strategy_id="S1",
        )
        store._total_realized_pnl_scaled = -50_000

        # pnl_delta == 0 is the *normal* opening-fill path: Position.update only
        # moves realized_pnl_scaled when a position closes.
        store._update_portfolio_aggregates(pnl_delta=0)

        assert store._total_realized_pnl_scaled == -50_000

    def test_recompute_still_counts_live_and_evicted_positions(self) -> None:
        """Guard against the fix double-counting or dropping the other sources."""
        store = PositionStore()
        store.on_fill(_fill(qty=1))
        store.positions[next(iter(store.positions))].realized_pnl_scaled = -1_000
        store._evicted_realized_pnl_scaled = -2_000
        store.load_recovery(
            account_id="ACC",
            symbol="TMFE6",
            net_qty=1,
            avg_price_scaled=1,
            realized_pnl_scaled=-4_000,
            strategy_id="S2",
        )

        store._update_portfolio_aggregates(pnl_delta=0)

        assert store._total_realized_pnl_scaled == -7_000


class TestUnknownCostBasisSentinel:
    def test_broker_created_entry_uses_the_negative_sentinel(self) -> None:
        """A broker qty with no cost basis must be -1, never 0.

        Three consumers detect "unknown basis" with ``avg_price_scaled < 0``
        (checkpoint.py, positions.py). A 0 reads as a genuine fill at price
        zero, so unrealized PnL accrues at mid x qty x multiplier every tick
        and the first close books the whole notional as profit.
        """
        verifier = StartupPositionVerifier(client=object(), position_store=PositionStore())
        merged: dict = {}

        verifier._distribute_correction([], target_qty=3, account_id="ACC", merged=merged, symbol="TXFE6")

        key = f"ACC:{MANUAL_STRATEGY_ID}:TXFE6"
        assert merged[key]["net_qty"] == 3
        assert merged[key]["avg_price_scaled"] < 0


@pytest.mark.skipif(PositionStore()._rust_tracker is None, reason="Rust position tracker not built")
class TestClearAlsoClearsTheRustTracker:
    def test_clearing_a_symbol_removes_it_from_the_rust_tracker(self) -> None:
        """clear_symbol_positions is how _auto_correct_drift breaks a drift loop.

        Clearing only the Python dict left the Rust tracker -- which owns the
        authoritative net_qty on the fast path -- holding the phantom quantity,
        so the very next fill resurrected it and the drift loop could not end.
        """
        store = PositionStore()
        store.on_fill(_fill(qty=5))
        key = next(iter(store.positions))
        assert store._rust_tracker.get(key)[0] == 5

        store.clear_symbol_positions("TXFE6")

        assert store._rust_tracker.get(key)[0] == 0

    def test_a_fill_after_clearing_starts_from_zero(self) -> None:
        """The observable consequence, stated as behaviour rather than state."""
        store = PositionStore()
        store.on_fill(_fill(qty=5))
        store.clear_symbol_positions("TXFE6")

        delta = store.on_fill(_fill(qty=1, price=1_810_000))

        assert delta.net_qty == 1

    def test_reset_empties_the_rust_tracker(self) -> None:
        store = PositionStore()
        store.on_fill(_fill(symbol="TXFE6", qty=3))
        store.on_fill(_fill(symbol="TMFE6", qty=2, strategy_id="S2"))
        assert store._rust_tracker.len() == 2

        store.reset()

        assert store._rust_tracker.len() == 0


class TestCheckpointSnapshotsBothViewsAtomically:
    def test_checkpoint_takes_positions_and_recovery_under_one_lock(self, tmp_path) -> None:
        """A key popped from recovery into positions between two separate reads
        lands in neither, and the checkpoint is silently written without it.

        Simulated by making the pop happen exactly at the moment the writer has
        finished reading positions -- which is what a concurrent
        ``_seed_from_recovery`` on the fill thread does.
        """
        store = PositionStore()
        store.load_recovery(
            account_id="ACC",
            symbol="TXFE6",
            net_qty=4,
            avg_price_scaled=1_800_000,
            strategy_id="S1",
        )
        recovery_key = next(iter(store._recovery_positions))

        real_snapshot = store.snapshot_positions

        def snapshot_then_pop() -> dict:
            result = real_snapshot()
            # The window: recovery entry moves into positions after the
            # positions view was taken. A second, unlocked read of
            # _recovery_positions would now miss it too.
            rdata = store._recovery_positions.pop(recovery_key)
            store.positions[recovery_key] = type(
                "P",
                (),
                {
                    "symbol": rdata["symbol"],
                    "net_qty": rdata["net_qty"],
                    "avg_price_scaled": rdata["avg_price_scaled"],
                    "realized_pnl_scaled": 0,
                    "fees_scaled": 0,
                },
            )()
            return result

        store.snapshot_positions = snapshot_then_pop  # type: ignore[method-assign]

        path = tmp_path / "positions.json"
        writer = PositionCheckpointWriter(store, path=str(path), interval_s=60)
        writer.write_checkpoint()

        payload = json.loads(path.read_text())
        assert recovery_key in payload["positions"], "position vanished from the checkpoint"
        assert payload["positions"][recovery_key]["net_qty"] == 4

    def test_checkpoint_survives_a_concurrent_recovery_pop(self, tmp_path) -> None:
        """Stress guard, not a pre-fix regression test.

        The loud half of the defect -- iterating the live dict while the fill
        thread pops raises RuntimeError, swallowed as checkpoint_write_failed --
        is a narrow window this cannot hit deterministically, so this test also
        passes against the unfixed tree. It is kept to catch a future
        reintroduction of unlocked iteration under sustained load; the silent
        half is pinned deterministically by the test above.
        """
        store = PositionStore()
        for i in range(200):
            store.load_recovery(
                account_id="ACC",
                symbol=f"SYM{i}",
                net_qty=1,
                avg_price_scaled=100,
                strategy_id="S1",
            )

        stop = threading.Event()

        def popper() -> None:
            while not stop.is_set():
                with store._fill_lock:
                    if store._recovery_positions:
                        store._recovery_positions.pop(next(iter(store._recovery_positions)))
                    else:
                        break

        path = tmp_path / "positions.json"
        writer = PositionCheckpointWriter(store, path=str(path), interval_s=60)

        t = threading.Thread(target=popper, daemon=True)
        t.start()
        try:
            for _ in range(50):
                writer.write_checkpoint()
        finally:
            stop.set()
            t.join(timeout=2)

        assert json.loads(path.read_text())["sha256"]


def _closing_fill(symbol: str, price: int, *, strategy_id: str = "S1") -> FillEvent:
    return FillEvent(
        fill_id=f"C-{symbol}-{price}",
        order_id="O-2",
        symbol=symbol,
        side=Side.SELL,
        qty=1,
        price=price,
        fee=0,
        tax=0,
        ingest_ts_ns=2,
        match_ts_ns=2,
        strategy_id=strategy_id,
        account_id="ACC",
    )


class TestEvictionForgetsEveryStore:
    """Eviction banks a position's *cumulative* realized PnL and deletes the
    Python entry. Two other stores are keyed the same way and were left behind.
    """

    def test_evicting_a_flat_position_does_not_double_count_its_realized_pnl(self) -> None:
        store = PositionStore()
        if store._rust_tracker is None:
            pytest.skip("Rust tracker unavailable; the double-count is Rust-path only")

        store.on_fill(_fill("TXFE6", price=1_800_000))
        store.on_fill(_closing_fill("TXFE6", price=1_810_000))
        banked = store.total_pnl
        assert banked != 0, "closing fill must realize PnL or the test proves nothing"

        store._evict_flat_positions()
        assert "ACC:S1:TXFE6" not in store.positions
        assert store._evicted_realized_pnl_scaled == banked

        # Reopening the same key takes the O(n) recompute branch, because
        # Position.update only moves realized_pnl_scaled on a close.
        store.on_fill(_fill("TXFE6", price=1_800_000))
        assert store.total_pnl == banked

    def test_eviction_removes_the_key_from_the_rust_tracker(self) -> None:
        store = PositionStore()
        if store._rust_tracker is None:
            pytest.skip("Rust tracker unavailable")

        store.on_fill(_fill("TXFE6", price=1_800_000))
        store.on_fill(_closing_fill("TXFE6", price=1_810_000))
        before = store._rust_tracker.len()

        store._evict_flat_positions()

        assert store._rust_tracker.len() == before - 1
        assert store._rust_tracker.get("ACC:S1:TXFE6") == (0, 0, 0, 0)

    def test_forgetting_a_key_drops_its_recovery_offsets(self) -> None:
        store = PositionStore()
        store.load_recovery(
            account_id="ACC",
            symbol="TXFE6",
            net_qty=1,
            avg_price_scaled=1_800_000,
            realized_pnl_scaled=-70_000,
            strategy_id="S1",
        )
        store.on_fill(_fill("TXFE6", price=1_800_000))
        key = "ACC:S1:TXFE6"
        assert store._recovery_rpnl_offsets.get(key) == -70_000

        store.clear_symbol_positions("TXFE6")

        assert key not in store._recovery_rpnl_offsets
        assert key not in store._recovery_fees_offsets


class TestCardinalityCapAppliesToBothFillPaths:
    """``HFT_POSITIONS_MAX_SIZE`` is the documented exposure cap (default
    10,000). The tracker is selected once in ``__init__``, so when it is
    available the Python path never runs and the cap had no effect at all.
    """

    def test_the_cap_is_enforced_when_the_rust_tracker_handles_the_fill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_POSITIONS_MAX_SIZE", "3")
        store = PositionStore()
        if store._rust_tracker is None:
            pytest.skip("Rust tracker unavailable; the Python path already capped")
        assert store._positions_max_size == 3

        for i in range(3):
            sym = f"SYM{i}"
            store.on_fill(_fill(sym, price=1_800_000))
            store.on_fill(_closing_fill(sym, price=1_800_000))
        assert len(store.positions) == 3

        store.on_fill(_fill("SYM9", price=1_800_000))

        assert len(store.positions) <= 3
        assert "ACC:S1:SYM9" in store.positions
