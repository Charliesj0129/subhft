"""PositionStore readers that iterate the live maps must hold ``_fill_lock``.

``on_fill_async`` runs ``on_fill`` on an ``asyncio.to_thread`` worker, so a fill can
insert into ``positions`` (or pop from ``_recovery_positions``) while another
thread is part-way through iterating it. ``net_qty_for_symbol`` and
``mark_to_market`` used to iterate both maps without the lock and could raise
"dictionary changed size during iteration".

The deterministic tests force the interleaving instead of hoping for it: the map
under test is a ``dict`` subclass whose ``items()`` starts a fill on a second
thread right after yielding its first entry, and waits briefly for that fill to
finish. An unlocked reader sees the map change under its iterator; a locked
reader holds the lock, so the fill cannot finish until the read is over.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.execution import FillEvent, Side
from hft_platform.execution.positions import PositionStore

#: How long the iteration hook waits for the competing fill to finish. A locked
#: reader never lets it finish, so this is also the per-test cost of the fix.
_FILL_WAIT_S = 0.05
_THREAD_JOIN_S = 5.0


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> PositionStore:
    """A pure-Python store (no Rust tracker, no metrics) with multiplier 1."""
    monkeypatch.setenv("HFT_RUST_POSITIONS", "0")
    monkeypatch.setattr(
        "hft_platform.observability.metrics.MetricsRegistry.get",
        staticmethod(lambda: None),
    )
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
    monkeypatch.setattr("hft_platform.execution.positions._RustPositionTracker", None)
    s = PositionStore()
    s._rust_tracker = None
    s.metrics = None
    s._positions_max_size = 1_000_000
    return s


def _fill(
    symbol: str,
    side: Side = Side.BUY,
    qty: int = 1,
    price: int = 1_000_000,
    strategy_id: str = "strat1",
    fill_id: str = "F",
) -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        account_id="acct1",
        order_id="ORD",
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=0,
        tax=0,
        ingest_ts_ns=1_000_000_000,
        match_ts_ns=1_000_000_000,
    )


def _seed_book(store: PositionStore) -> None:
    """Three live positions and two parked recovery lots (prices scaled x10000)."""
    store.on_fill(_fill("S1", Side.BUY, 3, 1_000_000, "strat1", "seed-1"))
    store.on_fill(_fill("S1", Side.BUY, 2, 1_050_000, "strat2", "seed-2"))
    store.on_fill(_fill("SYM2", Side.SELL, 1, 2_000_000, "strat1", "seed-3"))
    store.load_recovery("acct1", "R1", 4, 900_000)
    store.load_recovery("acct1", "R2", 7, 900_000)


_MIDS = {"S1": 1_100_000, "SYM2": 1_900_000, "R1": 1_000_000, "NEW": 1_000_000}

#: Hand-computed from ``_seed_book`` and ``_MIDS`` (multiplier 1):
#: S1/strat1 (1_100_000-1_000_000)*3 + S1/strat2 (1_100_000-1_050_000)*2
#: + SYM2 short (1_900_000-2_000_000)*-1 + R1 recovery (1_000_000-900_000)*4;
#: R2 has no mid so it contributes 0.
_BOOK_MTM = 300_000 + 100_000 + 100_000 + 400_000


class _HookedDict(dict):
    """A dict whose ``items()`` runs ``on_first_item`` once, after its first entry."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.on_first_item: Callable[[], None] | None = None

    def items(self) -> Any:
        raw = iter(dict.items(self))
        first = True
        for entry in raw:
            yield entry
            if first:
                first = False
                hook, self.on_first_item = self.on_first_item, None
                if hook is not None:
                    hook()


class _CompetingFill:
    """A fill applied on a second thread the moment the hook fires."""

    def __init__(self, store: PositionStore, fill: FillEvent) -> None:
        self.started = threading.Event()
        self.done = threading.Event()
        self.errors: list[BaseException] = []
        self._store = store
        self._fill = fill
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        self.started.set()
        try:
            self._store.on_fill(self._fill)
        except BaseException as exc:
            self.errors.append(exc)
        finally:
            self.done.set()

    def fire(self) -> None:
        self.thread.start()
        self.started.wait(_THREAD_JOIN_S)
        self.done.wait(_FILL_WAIT_S)

    def finish(self) -> None:
        self.thread.join(_THREAD_JOIN_S)


def _net_qty(store: PositionStore, symbol: str) -> Callable[[], int]:
    return lambda: store.net_qty_for_symbol(symbol)


def _mtm(store: PositionStore) -> Callable[[], int]:
    return lambda: store.mark_to_market(_MIDS)


_READERS = pytest.mark.parametrize("reader_name", ["net_qty_for_symbol", "mark_to_market"])
_MAPS = pytest.mark.parametrize("map_name", ["positions", "_recovery_positions"])


def _reader_and_expected(store: PositionStore, reader_name: str, map_name: str) -> tuple[Callable[[], int], int]:
    """The reader under test and the value it must return from the pre-fill book."""
    if reader_name == "mark_to_market":
        return _mtm(store), _BOOK_MTM
    # The competing fill adds to NEW (positions case) or to R1 (recovery case).
    symbol = "NEW" if map_name == "positions" else "R1"
    return _net_qty(store, symbol), 0 if map_name == "positions" else 4


class TestReadersAreSerialisedAgainstFills:
    @_READERS
    @_MAPS
    def test_reader_does_not_raise_when_a_fill_lands_mid_iteration(
        self, store: PositionStore, reader_name: str, map_name: str
    ) -> None:
        """RED before the fix: the fill changes the map size under the reader's iterator.

        GREEN after: the reader holds ``_fill_lock`` for the whole walk, the fill
        waits for it, and the reader returns the book as it was before the fill.
        """
        _seed_book(store)
        hooked = _HookedDict(getattr(store, map_name))
        setattr(store, map_name, hooked)
        if map_name == "positions":
            fill = _fill("NEW", Side.BUY, 10, 900_000, "strat1", "grow-new")
        else:
            # Matches the parked "acct1:R1" lot, so on_fill pops it from the recovery map.
            fill = _fill("R1", Side.BUY, 1, 1_100_000, "strat1", "pop-recovery")
        competing = _CompetingFill(store, fill)
        hooked.on_first_item = competing.fire
        reader, expected = _reader_and_expected(store, reader_name, map_name)

        errors: list[BaseException] = []
        value: int | None = None
        try:
            value = reader()
        except RuntimeError as exc:
            errors.append(exc)
        competing.finish()

        assert competing.started.is_set(), "competing fill never started: the hook did not fire"
        assert errors == [], f"{reader_name} raced a fill on {map_name}: {errors!r}"
        assert value == expected
        assert not competing.thread.is_alive(), "fill never finished: lock was not released by the reader"
        assert competing.errors == []
        assert competing.done.is_set()

    @_READERS
    def test_reader_releases_the_lock_for_the_next_fill(self, store: PositionStore, reader_name: str) -> None:
        _seed_book(store)
        reader, expected = _reader_and_expected(store, reader_name, "positions")

        assert reader() == expected
        acquired = store._fill_lock.acquire(blocking=False)
        if acquired:
            store._fill_lock.release()

        assert acquired, f"{reader_name} left _fill_lock held"


class TestReadersUnderSustainedFills:
    def test_readers_do_not_raise_while_a_worker_grows_the_book(self, store: PositionStore) -> None:
        """Bounded stress: many fills on distinct symbols against both readers.

        Complements the deterministic test above; on its own it only fails when
        the scheduler lands a switch inside an iteration.
        """
        _seed_book(store)
        stop = threading.Event()
        writer_errors: list[BaseException] = []
        applied = 0

        def _writer() -> None:
            nonlocal applied
            try:
                for i in range(3000):
                    if stop.is_set():
                        break
                    store.on_fill(_fill(f"GROW{i}", Side.BUY, 1, 1_000_000, "strat1", f"grow-{i}"))
                    applied += 1
            except BaseException as exc:
                writer_errors.append(exc)
            finally:
                stop.set()

        worker = threading.Thread(target=_writer, daemon=True)
        reader_errors: list[BaseException] = []
        reads = 0
        worker.start()
        try:
            while not stop.is_set() and reads < 20_000:
                try:
                    store.net_qty_for_symbol("S1")
                    store.mark_to_market(_MIDS)
                except RuntimeError as exc:
                    reader_errors.append(exc)
                    break
                reads += 1
        finally:
            stop.set()
            worker.join(_THREAD_JOIN_S)

        assert reader_errors == [], f"reader raced the fill worker after {reads} reads: {reader_errors!r}"
        assert writer_errors == []
        assert reads > 0
        assert applied > 0
        assert not worker.is_alive()


class TestReadersKeepTheirValues:
    """Behaviour is unchanged for a fixed fill set, with no lock held by the caller."""

    @staticmethod
    def _run_with_deadline(fn: Callable[[], int]) -> int:
        box: list[int] = []
        thread = threading.Thread(target=lambda: box.append(fn()), daemon=True)
        thread.start()
        thread.join(_THREAD_JOIN_S)
        assert not thread.is_alive(), "reader did not return: deadlock on _fill_lock"
        return box[0]

    @pytest.mark.parametrize(
        ("symbol", "strategy_id", "expected"),
        [
            ("S1", None, 5),  # 3 + 2 across strategies
            ("S1", "strat1", 3),
            ("S1", "strat2", 2),
            ("S1", "other", 0),
            ("SYM2", None, -1),
            ("R1", None, 4),  # parked recovery lot, aggregate view
            ("R1", "strat1", 0),  # legacy lot with no strategy_id is excluded from a filtered query
            ("R2", None, 7),
            ("MISSING", None, 0),
        ],
    )
    def test_net_qty_for_symbol_returns_the_book_quantity(
        self, store: PositionStore, symbol: str, strategy_id: str | None, expected: int
    ) -> None:
        _seed_book(store)

        got = self._run_with_deadline(lambda: store.net_qty_for_symbol(symbol, strategy_id))

        assert got == expected

    def test_mark_to_market_returns_the_book_unrealized_pnl(self, store: PositionStore) -> None:
        _seed_book(store)

        got = self._run_with_deadline(lambda: store.mark_to_market(_MIDS))

        assert got == _BOOK_MTM

    def test_mark_to_market_skips_unknown_cost_basis_recovery(self, store: PositionStore) -> None:
        store.load_recovery("acct1", "UNK", 5, -1)

        got = self._run_with_deadline(lambda: store.mark_to_market({"UNK": 1_000_000}))

        assert got == 0
