"""Wave 3 — PositionStore reader lock expansion regression tests.

Each test demonstrates a race or torn-read against a writer (on_fill running
in a background thread, mirroring on_fill_async's `asyncio.to_thread` usage)
and a reader (MtM.calculate, _build_positions_by_strategy, _get_symbol_net_qty,
get_drawdown_pct, router pre_realized snapshot).

These are concurrent stress tests using threading.Thread (NOT asyncio).
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.execution import FillEvent, Side
from hft_platform.execution.mtm import MarkToMarketCalculator
from hft_platform.execution.positions import Position, PositionStore

# ---------------------------------------------------------------------------
# Fixtures (mirror tests/unit/test_position_store_unit.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_rust(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the pure-Python tracker so writers exercise self.positions[].

    Race tests need writes to land in self.positions (not the Rust tracker)
    so that concurrent dict-iteration races are reproducible.
    """
    monkeypatch.setenv("HFT_RUST_POSITIONS", "0")
    monkeypatch.setattr(
        "hft_platform.observability.metrics.MetricsRegistry.get",
        staticmethod(lambda: None),
    )


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> PositionStore:
    mock_metadata = MagicMock()
    mock_metadata.price_scale.return_value = 10_000
    mock_metadata.contract_multiplier.return_value = 1
    monkeypatch.setattr(
        "hft_platform.execution.positions.SymbolMetadata",
        lambda *a, **kw: mock_metadata,
    )
    mock_provider = MagicMock()
    mock_provider.scale_for.return_value = 10_000
    monkeypatch.setattr(
        "hft_platform.execution.positions.SymbolMetadataPriceScaleProvider",
        lambda *a, **kw: mock_provider,
    )
    monkeypatch.setattr(
        "hft_platform.execution.positions._RustPositionTracker",
        None,
    )
    s = PositionStore()
    # Hard-disable any rust tracker that may have leaked in.
    s._rust_tracker = None
    return s


def _make_fill(
    *,
    side: Side = Side.BUY,
    qty: int = 1,
    price: int = 1000_0000,
    fee: int = 0,
    tax: int = 0,
    account_id: str = "acct1",
    strategy_id: str = "strat1",
    symbol: str = "SYM1",
    match_ts_ns: int = 1_000_000_000,
    fill_id: str = "F",
) -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        account_id=account_id,
        order_id="ORD",
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        tax=tax,
        ingest_ts_ns=match_ts_ns,
        match_ts_ns=match_ts_ns,
    )


# Common writer driver: mutates `positions` from a background thread to
# induce dict-iteration races.  Aggressively add+pop keys under
# `_fill_lock` so the dict size oscillates: this is the only way to
# trigger CPython's "dictionary changed size during iteration"
# RuntimeError in an unprotected reader.
#
# Strategy: keep a working set of N entries, repeatedly delete one and
# add another DIFFERENT key.  Each individual mutation can land between
# two `__next__` calls of an unprotected `for k in dict.items()` loop in
# the reader — CPython detects size-change deltas and raises RuntimeError.
def _writer_loop(
    store: PositionStore,
    stop: threading.Event,
    iterations: int = 50000,
) -> list[BaseException]:
    errors: list[BaseException] = []
    try:
        # Pre-fill working set so reader iteration is non-trivial.
        with store._fill_lock:
            for j in range(50):
                key0 = f"acct1:strat1:WS{j}"
                store.positions[key0] = Position("acct1", "strat1", f"WS{j}", net_qty=1)
        counter = 1000
        for _ in range(iterations):
            if stop.is_set():
                break
            try:
                # One mutation per acquire so reader has many windows to
                # observe a size change.
                old_key = f"acct1:strat1:WS{counter % 50}"
                new_sym = f"WX{counter}"
                new_key = f"acct1:strat1:{new_sym}"
                with store._fill_lock:
                    store.positions.pop(old_key, None)
                    store.positions[new_key] = Position(
                        "acct1", "strat1", new_sym, net_qty=1
                    )
                with store._fill_lock:
                    store.positions.pop(new_key, None)
                    store.positions[old_key] = Position(
                        "acct1", "strat1", f"WS{counter % 50}", net_qty=1
                    )
                counter += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                break
    finally:
        stop.set()
    return errors


# ---------------------------------------------------------------------------
# R3-1 — MtMCalculator.calculate must not race on store.positions iteration
# ---------------------------------------------------------------------------


class TestR3_1_MtMCalculator:
    def test_calculate_concurrent_with_fills_does_not_raise(
        self,
        store: PositionStore,
    ) -> None:
        """RED before fix: MtM iterates store.positions; writer mutates dict
        size mid-iteration and triggers RuntimeError.
        GREEN after fix: snapshot_positions() returns isolated dict copy.
        """
        # Pre-seed a couple of positions so first reads hit something.
        for sym in ("S0", "S1"):
            store.on_fill(_make_fill(symbol=sym, side=Side.BUY, fill_id=f"seed-{sym}"))

        calc = MarkToMarketCalculator(
            store,
            mid_price_fn=lambda _sym: 1000_0000,
            multiplier_fn=lambda _sym: 1,
        )

        stop = threading.Event()
        writer_errors: list[BaseException] = []

        def _writer() -> None:
            writer_errors.extend(_writer_loop(store, stop, iterations=50000))

        t = threading.Thread(target=_writer, daemon=True)
        t.start()
        try:
            reader_errors: list[BaseException] = []
            for _ in range(50000):
                try:
                    calc.calculate()
                except RuntimeError as exc:
                    reader_errors.append(exc)
                if stop.is_set():
                    break
        finally:
            stop.set()
            t.join(timeout=5.0)

        assert not reader_errors, f"MtM.calculate() raced: {reader_errors!r}"
        assert not writer_errors, f"writer crashed: {writer_errors!r}"


# ---------------------------------------------------------------------------
# R3-2 — _build_positions_by_strategy fallback dict(raw) must hold lock
# ---------------------------------------------------------------------------


class TestR3_2_BuildPositionsByStrategyFallback:
    def _runner_source(self) -> str:
        return Path(
            "/home/charlie/hft_platform/src/hft_platform/strategy/runner.py"
        ).read_text()

    def test_fallback_dict_copy_is_lock_guarded(self) -> None:
        """Source-level audit: `dict(raw)` fallback in
        `_build_positions_by_strategy` must be wrapped in a `with
        position_store._fill_lock:` block (with hasattr guard for mocks).

        Behavioural racing of `dict(raw)` is hard to reproduce under
        CPython's GIL because `dict(another_dict)` is a single C call
        that holds the GIL throughout — but free-threaded Python
        (PEP 703) and any future weakening of the GIL makes the lock
        wrap mandatory.
        """
        src = self._runner_source()
        # Find the fallback block: after `if hasattr(... "snapshot_positions"):`
        # the else: branch contains `raw = getattr(... "positions", None)`
        # then `raw = dict(raw)`.
        m = re.search(
            r'if hasattr\(self\.position_store, "snapshot_positions"\):'
            r".*?"
            r"raw = dict\(raw\)",
            src,
            re.DOTALL,
        )
        assert m is not None, "fallback `raw = dict(raw)` block not found"
        block = m.group(0)
        # The dict-copy site MUST be inside a `with ... _fill_lock` block.
        # Look for the pattern in the fallback branch.
        assert "_fill_lock" in block, (
            "fallback `raw = dict(raw)` block is not guarded by "
            f"_fill_lock: {block!r}"
        )

    def test_fallback_dict_copy_runs_without_error(
        self, store: PositionStore
    ) -> None:
        """Behavioural smoke-check that the fallback path still works
        after the lock guard is added (no deadlock, no AttributeError on
        mocks without _fill_lock)."""

        class _Wrapper:
            def __init__(self, real: PositionStore) -> None:
                self._real = real
                self._fill_lock = real._fill_lock
                self._rust_tracker = None
                self._recovery_positions = real._recovery_positions

            @property
            def positions(self):  # type: ignore[no-untyped-def]
                return self._real.positions

        from hft_platform.strategy.runner import StrategyRunner

        wrapper = _Wrapper(store)
        runner = StrategyRunner.__new__(StrategyRunner)
        runner.position_store = wrapper
        runner._position_key_cache = {}
        store.on_fill(_make_fill(symbol="S0", fill_id="seed"))
        result = runner._build_positions_by_strategy()
        assert isinstance(result, dict)

    def test_fallback_works_without_fill_lock_attr(
        self, store: PositionStore
    ) -> None:
        """Backward-compat: a position_store mock without _fill_lock must
        still work (hasattr guard)."""

        class _MockNoLock:
            def __init__(self) -> None:
                self.positions = {
                    "acct1:strat1:X": Position("acct1", "strat1", "X", net_qty=2),
                }
                self._rust_tracker = None
                # No _fill_lock attribute, no snapshot_positions method.
                self._recovery_positions = {}

        from hft_platform.strategy.runner import StrategyRunner

        runner = StrategyRunner.__new__(StrategyRunner)
        runner.position_store = _MockNoLock()
        runner._position_key_cache = {}
        result = runner._build_positions_by_strategy()
        assert result == {"strat1": {"X": 2}}


# ---------------------------------------------------------------------------
# R3-3 — _recovery_positions iteration must be snapshotted under lock
# ---------------------------------------------------------------------------


class TestR3_3_RecoveryPositionsRead:
    def test_recovery_positions_iteration_does_not_race(
        self,
        store: PositionStore,
    ) -> None:
        """Writers mutate _recovery_positions inside on_fill (pop) under
        _fill_lock. Reader iterates without lock -> RuntimeError."""
        from hft_platform.strategy.runner import StrategyRunner

        runner = StrategyRunner.__new__(StrategyRunner)
        runner.position_store = store
        runner._position_key_cache = {}

        # Pre-seed _recovery_positions with many entries; writer keeps adding
        # via on_fill_recovery_position so reader observes a moving target.
        for i in range(20):
            store._recovery_positions[f"acct1:strat1:S{i}"] = {
                "net_qty": 1,
                "avg_price": 1000_0000,
                "symbol": f"S{i}",
                "strategy_id": "strat1",
            }

        stop = threading.Event()
        writer_errors: list[BaseException] = []

        def _recovery_writer() -> None:
            try:
                for i in range(5000):
                    if stop.is_set():
                        break
                    key = f"acct1:strat1:R{i % 100}"
                    # Mutate under lock the same way on_fill does.
                    with store._fill_lock:
                        store._recovery_positions[key] = {
                            "net_qty": (i % 5) + 1,
                            "avg_price": 1000_0000,
                            "symbol": f"R{i % 100}",
                            "strategy_id": "strat1",
                        }
                        if i % 7 == 0 and store._recovery_positions:
                            # Pop a random key (mirrors on_fill behaviour).
                            for k in list(store._recovery_positions):
                                store._recovery_positions.pop(k, None)
                                break
            except Exception as exc:  # noqa: BLE001
                writer_errors.append(exc)
            finally:
                stop.set()

        t = threading.Thread(target=_recovery_writer, daemon=True)
        t.start()
        try:
            reader_errors: list[BaseException] = []
            for _ in range(1000):
                try:
                    runner._build_positions_by_strategy()
                except RuntimeError as exc:
                    reader_errors.append(exc)
                if stop.is_set():
                    break
        finally:
            stop.set()
            t.join(timeout=5.0)

        assert not reader_errors, f"recovery iteration raced: {reader_errors!r}"
        assert not writer_errors, f"writer crashed: {writer_errors!r}"


# ---------------------------------------------------------------------------
# R3-4 — _get_symbol_net_qty must snapshot positions under lock
# ---------------------------------------------------------------------------


class TestR3_4_GetSymbolNetQty:
    def test_get_symbol_net_qty_does_not_race(self, store: PositionStore) -> None:
        from hft_platform.strategy.runner import _get_symbol_net_qty

        # Pre-seed.
        for sym in ("S0", "S1"):
            store.on_fill(_make_fill(symbol=sym, fill_id=f"seed-{sym}"))

        stop = threading.Event()
        writer_errors: list[BaseException] = []

        def _writer() -> None:
            writer_errors.extend(_writer_loop(store, stop, iterations=50000))

        t = threading.Thread(target=_writer, daemon=True)
        t.start()
        try:
            reader_errors: list[BaseException] = []
            for _ in range(50000):
                try:
                    _get_symbol_net_qty(store, "S0", strategy_id="strat1")
                except RuntimeError as exc:
                    reader_errors.append(exc)
                if stop.is_set():
                    break
        finally:
            stop.set()
            t.join(timeout=5.0)

        assert not reader_errors, f"_get_symbol_net_qty raced: {reader_errors!r}"
        assert not writer_errors, f"writer crashed: {writer_errors!r}"


# ---------------------------------------------------------------------------
# R3-5 — Router _pre_realized snapshot reads must hold _fill_lock
# ---------------------------------------------------------------------------


class TestR3_5_RouterPreRealizedSnapshot:
    """Source-level audit: the three router sites that read `_pre_pos =
    self.position_store.positions.get(_pos_key)` followed by
    `_pre_pos.realized_pnl_scaled` must be wrapped in
    `with self.position_store._fill_lock:` to snapshot the int atomically
    before any concurrent on_fill_async writer can clobber it.
    """

    def _router_source(self) -> str:
        return Path(
            "/home/charlie/hft_platform/src/hft_platform/execution/router.py"
        ).read_text()

    def test_pre_realized_reads_are_lock_guarded(self) -> None:
        src = self._router_source()
        # Each _pre_pos snapshot site MUST have _fill_lock in its look-back
        # window (either holding the lock directly, or as part of the
        # hasattr-guarded fallback). After the Wave 3 fix, each of the
        # three logical sites expands to two `positions.get(_pos_key)`
        # calls (one inside the `with _fill_lock:` block, one in the else
        # fallback for mocks). Both occurrences must reside in a method
        # body that references _fill_lock within ~400 chars before the call.
        get_calls = [
            (m.start(), m.end())
            for m in re.finditer(
                r"self\.position_store\.positions\.get\(_pos_key", src
            )
        ]
        pre_pos_sites = [
            (s, e)
            for (s, e) in get_calls
            if "_pre_pos" in src[max(0, s - 80) : s]
        ]
        # Pre-fix: 3 sites. Post-fix: 6 sites (each expands to lock + else).
        assert len(pre_pos_sites) >= 3, (
            f"expected >= 3 _pre_pos snapshot sites, found {len(pre_pos_sites)}"
        )

        for s, _e in pre_pos_sites:
            window = src[max(0, s - 400) : s]
            assert "_fill_lock" in window, (
                f"_pre_pos snapshot at offset {s} not guarded by "
                f"_fill_lock; window={window!r}"
            )


# ---------------------------------------------------------------------------
# R3-6 — get_drawdown_pct must read peak/total under lock to avoid torn pair
# ---------------------------------------------------------------------------


class TestR3_6_GetDrawdownPct:
    def _positions_source(self) -> str:
        return Path(
            "/home/charlie/hft_platform/src/hft_platform/execution/positions.py"
        ).read_text()

    def test_drawdown_read_pair_is_lock_guarded(self) -> None:
        """Source-level audit: get_drawdown_pct's read of
        _peak_equity_scaled and _total_realized_pnl_scaled (and the
        compare) must be inside `with self._fill_lock:` so the pair
        snapshot is atomic relative to writers.
        """
        src = self._positions_source()
        m = re.search(
            r"def get_drawdown_pct\(self\).*?(?=\n    def )",
            src,
            re.DOTALL,
        )
        assert m is not None, "get_drawdown_pct not found"
        body = m.group(0)
        assert "_fill_lock" in body, (
            "get_drawdown_pct does not acquire _fill_lock; body=" + body
        )

    def test_drawdown_returns_consistent_pair(self, store: PositionStore) -> None:
        """Behavioural check: every observed dd value MUST correspond to a
        (peak, current) pair the writer set atomically.

        Without the lock, reader can interleave reads across two writes
        and observe a (peak, current) pair that was never set
        atomically — caught by checking dd against the set of valid
        regime dd values."""
        store._peak_equity_scaled = 20_000_000_000
        store._total_realized_pnl_scaled = 1
        regimes = [
            (20_000_000_000, 20_000_000_000),  # dd=0.0
            (20_000_000_000, 1),  # dd ~= 1.0
        ]
        valid_dds = {
            0.0,
            (20_000_000_000 - 1) / 20_000_000_000,
        }

        stop = threading.Event()
        writer_errors: list[BaseException] = []

        def _writer() -> None:
            try:
                for i in range(20000):
                    if stop.is_set():
                        break
                    p, c = regimes[i % 2]
                    with store._fill_lock:
                        store._peak_equity_scaled = p
                        store._total_realized_pnl_scaled = c
            except Exception as exc:  # noqa: BLE001
                writer_errors.append(exc)
            finally:
                stop.set()

        t = threading.Thread(target=_writer, daemon=True)
        t.start()
        observations: list[float] = []
        try:
            for _ in range(20000):
                observations.append(store.get_drawdown_pct())
                if stop.is_set():
                    break
        finally:
            stop.set()
            t.join(timeout=5.0)

        # Every observation must be a valid regime dd, OR exactly 0.0
        # (cold-start guard at start) — not some intermediate torn value.
        invalid = [
            dd
            for dd in observations
            if not any(abs(dd - v) < 1e-9 for v in valid_dds)
        ]
        assert not invalid, (
            f"observed {len(invalid)} torn dd values not in regime set "
            f"{valid_dds}: first 5={invalid[:5]!r}"
        )
        assert not writer_errors, f"writer crashed: {writer_errors!r}"


# ---------------------------------------------------------------------------
# R3-3 hole — atomic snapshot of positions + recovery
#
# Codex stop-time review (2026-04-25) caught: _build_positions_by_strategy
# takes the positions snapshot and the recovery snapshot in TWO separate
# _fill_lock acquisitions. _seed_from_recovery (positions.py:347-379) runs
# inside the writer's critical section and atomically pops a recovery
# entry into self.positions. If a writer interleaves between the reader's
# two lock acquisitions, the entry vanishes from BOTH snapshots: it was
# popped from recovery AFTER the positions snapshot (so missing there)
# but the recovery snapshot was taken AFTER the pop (so missing there too).
# Fix: snapshot_positions_with_recovery() returns both under one lock.


class TestSnapshotPositionsWithRecoveryAtomic:
    def test_recovery_does_not_disappear_during_concurrent_seed(
        self, store: PositionStore
    ) -> None:
        """The merged (positions + recovery) view must NEVER lose an entry.

        Race scenario (interleaved by hand via gates):
          1. Reader takes positions snapshot → empty
          2. Writer pops recovery, seeds positions[key]
          3. Reader takes recovery snapshot → empty
          4. Reader merges → entry LOST from both views

        With the fix (atomic snapshot under one lock), step 1+3 happen
        under one lock so the writer's pop+seed is either entirely before
        or entirely after the reader's snapshot — the entry appears in
        exactly one of the two views, never neither.
        """
        # Seed recovery with a known entry.
        store._recovery_positions["acct1:strat1:RECOV"] = {
            "account_id": "acct1",
            "strategy_id": "strat1",
            "symbol": "RECOV",
            "net_qty": 7,
            "avg_price_scaled": 1000_0000,
            "realized_pnl_scaled": 0,
            "fees_scaled": 0,
        }

        # The new public API must exist and atomically snapshot both.
        positions_snap, recovery_snap = store.snapshot_positions_with_recovery()
        # Pre-write: should appear in recovery, not in positions.
        assert "acct1:strat1:RECOV" not in positions_snap
        assert "acct1:strat1:RECOV" in recovery_snap

        # Now drive a fill that triggers _seed_from_recovery.
        fill = _make_fill(symbol="RECOV", qty=7, price=1000_0000)
        store.on_fill(fill)

        # After the seed, recovery must be empty and positions must contain
        # the merged key.
        positions_snap2, recovery_snap2 = store.snapshot_positions_with_recovery()
        assert "acct1:strat1:RECOV" in positions_snap2
        assert "acct1:strat1:RECOV" not in recovery_snap2

    def test_recovery_snapshot_isolates_inner_dict(
        self, store: PositionStore
    ) -> None:
        """Mutating the snapshot's recovery values must NOT affect the store."""
        store._recovery_positions["acct1:strat1:ISO"] = {
            "account_id": "acct1",
            "strategy_id": "strat1",
            "symbol": "ISO",
            "net_qty": 3,
        }
        _, recovery_snap = store.snapshot_positions_with_recovery()
        recovery_snap["acct1:strat1:ISO"]["net_qty"] = 999
        # Original must be unaffected.
        assert store._recovery_positions["acct1:strat1:ISO"]["net_qty"] == 3
