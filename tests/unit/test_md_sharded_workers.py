"""P3: symbol-sharded market-data worker threads (HFT_MD_WORKERS)."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.services._md_sharded import (
    CommittedMDView,
    MDWorkerCrashedError,
    ShardedMDPipeline,
    ShardWorker,
)

SYMBOLS = [f"S{i:03d}" for i in range(32)]


def _raw(sym: str, n: int) -> dict:
    return {"code": sym, "n": n}


def _compute(raw: dict, w: ShardWorker) -> tuple:
    return (raw["code"], raw, None, None, None, None, None, w.epoch)


async def _wait_until(pred, timeout_s: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not pred():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.001)


def _svc(monkeypatch, workers: str = "2", **kw):
    from hft_platform.services.market_data import MarketDataService

    monkeypatch.setenv("HFT_MD_WORKERS", workers)
    client = kw.pop("client", None) or SimpleNamespace()
    return MarketDataService(MagicMock(), asyncio.Queue(maxsize=16), client, **kw)


# -- ordering / conservation / budget / view ----------------------------------


def test_per_symbol_order_preserved_across_four_workers():
    async def main() -> dict[str, list[int]]:
        seen: dict[str, list[int]] = {s: [] for s in SYMBOLS}

        def commit(r: tuple) -> None:
            seen[r[0]].append(r[1]["n"])

        pipe = ShardedMDPipeline(4, 100_000, 256, _compute, commit)
        pipe.start(asyncio.get_running_loop())

        def produce(offset: int) -> None:
            for n in range(2000):
                for s in SYMBOLS[offset::2]:
                    pipe.offer(_raw(s, n))

        threads = [threading.Thread(target=produce, args=(k,)) for k in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        await _wait_until(lambda: pipe.committed == 2000 * len(SYMBOLS))
        pipe.stop()
        return seen

    seen = asyncio.run(main())
    for sym, ns in seen.items():
        assert ns == list(range(2000)), sym


def test_conservation_offered_equals_committed_plus_dropped_under_overflow():
    async def main() -> dict[str, int]:
        gate = threading.Event()

        def slow_compute(raw: dict, w: ShardWorker) -> tuple:
            gate.wait()
            return _compute(raw, w)

        pipe = ShardedMDPipeline(2, 8, 256, slow_compute, lambda r: None)
        pipe.start(asyncio.get_running_loop())
        for n in range(500):
            pipe.offer(_raw(SYMBOLS[n % 8], n))
        gate.set()
        await _wait_until(lambda: pipe.stats()["queued"] == 0 and pipe.stats()["pending_commit"] == 0)
        await asyncio.sleep(0.01)
        st = pipe.stats()
        pipe.stop()
        return st

    st = asyncio.run(main())
    assert st["offered"] == 500
    assert st["dropped"] > 0
    assert st["offered"] == st["committed"] + st["dropped"] + st["rejected"] + st["stale_drained"]


def test_overflow_drop_counts_per_worker_without_callback_per_drop():
    pipe = ShardedMDPipeline(1, 3, 256, _compute, lambda r: None)
    pipe._admitting = True  # admission open, workers not started: the inbox only fills
    accepted = [pipe.offer(_raw("A", n)) for n in range(6)]
    assert accepted == [True, True, True, False, False, False]
    assert pipe.drop_totals() == (3, 3)


def test_drop_poll_aggregates_across_shards_and_escalates_once(monkeypatch):
    svc = _svc(monkeypatch, "2")
    calls: list[int] = []
    svc._escalate_raw_drop = calls.append
    w0, w1 = svc._md_shards._workers
    w0.dropped, w0.consecutive_drops = 3, 3
    w1.dropped, w1.consecutive_drops = 5, 5
    svc._md_shard_poll_drops()
    svc._md_shard_poll_drops()  # nothing new: no second escalation
    assert calls == [8]
    assert svc._raw_dropped_count == 8
    assert svc._raw_consecutive_drops == 5


def test_worker_crash_notifies_supervisor_and_stops_admission():
    async def main() -> tuple[list, bool, bool]:
        crashes: list = []

        def bad_compute(raw: dict, w: ShardWorker) -> tuple:
            raise ValueError("boom")

        pipe = ShardedMDPipeline(2, 100, 256, bad_compute, lambda r: None, on_crash=lambda i, e: crashes.append(e))
        pipe.start(asyncio.get_running_loop())
        pipe.offer(_raw("A", 0))
        await _wait_until(lambda: bool(crashes))
        accepted_after = pipe.offer(_raw("A", 1))
        pipe.stop()
        return crashes, pipe.admitting, accepted_after

    crashes, admitting, accepted_after = asyncio.run(main())
    assert isinstance(crashes[0], ValueError)
    assert admitting is False
    assert accepted_after is False


def test_commit_budget_yields_fill_callback_runs_before_burst_finishes():
    async def main() -> tuple[int, int]:
        loop = asyncio.get_running_loop()
        committed_at_fill: list[int] = []
        holder: list[ShardedMDPipeline] = []
        gate = threading.Event()

        def gated_compute(raw: dict, w: ShardWorker) -> tuple:
            gate.wait()
            return _compute(raw, w)

        def commit(r: tuple) -> None:
            if r[1]["n"] == 10:
                # A fill/cancel arriving mid-burst: it must not wait for the whole burst.
                loop.call_soon(lambda: committed_at_fill.append(holder[0].committed))

        pipe = ShardedMDPipeline(1, 20_000, 256, gated_compute, commit)
        holder.append(pipe)
        pipe.start(loop)
        for n in range(10_000):
            pipe.offer(_raw("A", n))
        gate.set()
        await _wait_until(lambda: pipe.committed == 10_000)
        pipe.stop()
        return committed_at_fill[0], pipe.drain_turns

    at_fill, turns = asyncio.run(main())
    assert at_fill < 10_000
    assert turns >= 10_000 // 256


def test_committed_view_never_shows_half_applied_book_during_burst():
    async def main() -> list[tuple]:
        view = CommittedMDView()
        book = {"bid": 0, "ask": 1}

        def compute(raw: dict, w: ShardWorker) -> tuple:
            n = raw["n"]
            book["bid"] = n * 10
            book["ask"] = n * 10 + 1  # live state is half-applied between these two writes
            l1 = (n, book["bid"], book["ask"], 0, 0, 0, 0)
            return ("A", raw, None, None, l1, (n,), None, w.epoch)

        pipe = ShardedMDPipeline(1, 50_000, 64, compute, lambda r: view.publish(r[0], r))
        pipe.start(asyncio.get_running_loop())
        for n in range(20_000):
            pipe.offer(_raw("A", n))
        bad: list[tuple] = []
        while pipe.committed < 20_000:
            l1 = view.get_l1_scaled("A")
            ft = view.get_feature_tuple("A")
            if l1 is not None and (l1[2] != l1[1] + 1 or ft != (l1[0],)):
                bad.append((l1, ft))
            await asyncio.sleep(0)
        pipe.stop()
        return bad

    assert asyncio.run(main()) == []


def test_committed_view_book_snapshot_matches_lob_keys_from_committed_l1():
    view = CommittedMDView()
    l1 = (123, 1_000_000, 1_010_000, 2_010_000, 10_000, 7, 9)
    view.publish("TMFA6", ("TMFA6", None, None, None, l1, None, None, 0))
    snap = view.get_book_snapshot("TMFA6")
    assert snap == {
        "symbol": "TMFA6",
        "timestamp": 123,
        "best_bid": 1_000_000,
        "best_ask": 1_010_000,
        "mid_price": 1_005_000.0,
        "spread": 10_000.0,
        "mid_price_x2": 2_010_000,
        "spread_scaled": 10_000,
    }
    assert view.get_book_snapshot("NONE") is None


def test_committed_view_feature_view_reads_committed_meta_not_live_state():
    fe = MagicMock()
    fe._feature_set = SimpleNamespace(feature_set_id="fs1", schema_version=3)
    fe._feature_ids = ("a", "b")
    fe._index_by_id = {"a": 0, "b": 1}
    fe.active_profile_id.return_value = "p"
    view = CommittedMDView(fe)
    view.publish("X", ("X", None, None, None, None, (5, 6), (11, 22, 33, 4, 99), 0))
    fv = view.get_feature_view("X")
    assert fv["seq"] == 11 and fv["source_ts_ns"] == 22 and fv["local_ts_ns"] == 33
    assert fv["quality_flags"] == 4 and fv["values"] == (5, 6)
    assert view.get_feature("X", "b") == 6
    assert view.last_update_ns("X") == 99
    fe.get_feature_view.assert_not_called()


def test_runner_bind_committed_view_switches_every_md_source():
    from hft_platform.strategy.runner import StrategyRunner

    runner = StrategyRunner.__new__(StrategyRunner)
    runner._strat_executors = []
    view = CommittedMDView()
    runner.bind_committed_md_view(view)
    assert runner._lob_snapshot_source == view.get_book_snapshot
    assert runner._lob_l1_source == view.get_l1_scaled
    assert runner._feature_view_source == view.get_feature_view
    assert runner._feature_tuple_source == view.get_feature_tuple
    assert runner._feature_value_source == view.get_feature
    assert runner._feature_staleness_source == view.last_update_ns


def test_price_band_mid_from_committed_view_is_fresh_gated():
    from hft_platform.core import timebase
    from hft_platform.risk.validators import PriceBandValidator

    view = CommittedMDView()
    v = PriceBandValidator({"global_defaults": {}}, lob=view)
    now = timebase.now_ns()
    view.publish("A", ("A", None, None, None, (now, 100, 102, 202, 2, 1, 1), None, None, 0))
    assert v._get_mid_price("A") == 101
    view.publish("A", ("A", None, None, None, (now - 3_600 * 10**9, 100, 102, 202, 2, 1, 1), None, None, 0))
    assert v._get_mid_price("A") is None


# -- resets routed to owning workers --------------------------------------------


def _gated_pipeline(order: list, reset_calls: list, cleared: list):
    gate = threading.Event()

    def compute(raw: dict, w: ShardWorker) -> tuple:
        if raw["n"] == 0:
            gate.wait()
        return _compute(raw, w)

    def reset(symbols: set) -> None:
        reset_calls.append((threading.current_thread().name, set(symbols)))
        order.append(("reset", frozenset(symbols)))

    def on_cleared(symbols: frozenset) -> None:
        cleared.append(set(symbols))
        order.append(("cleared", symbols))

    pipe = ShardedMDPipeline(
        1,
        100,
        256,
        compute,
        lambda r: order.append((r[0], r[1]["n"])),
        reset=reset,
        on_reset_committed=on_cleared,
    )
    return pipe, gate


def test_reset_runs_on_owning_worker_without_blocking_loop():
    async def main() -> tuple[float, list]:
        order: list = []
        reset_calls: list = []
        cleared: list = []
        pipe, gate = _gated_pipeline(order, reset_calls, cleared)
        pipe.start(asyncio.get_running_loop())
        pipe.offer(_raw("A", 0))
        await asyncio.sleep(0.01)  # worker is now blocked inside compute(n=0)
        t0 = time.perf_counter()
        assert pipe.request_reset({"A"})
        elapsed = time.perf_counter() - t0
        gate.set()
        await _wait_until(lambda: cleared != [])
        pipe.stop()
        return elapsed, reset_calls

    elapsed, reset_calls = asyncio.run(main())
    assert elapsed < 0.01
    assert reset_calls == [("md-shard-0", {"A"})]


def test_reset_of_one_symbol_does_not_skip_queued_events_of_other_symbol_on_same_worker():
    async def main() -> tuple[list, list, dict]:
        order: list = []
        reset_calls: list = []
        cleared: list = []
        pipe, gate = _gated_pipeline(order, reset_calls, cleared)
        pipe.start(asyncio.get_running_loop())
        pipe.offer(_raw("B", 0))  # blocks the worker so everything below is queued
        for n in range(1, 4):
            pipe.offer(_raw("A", n))
            pipe.offer(_raw("B", n))
        assert pipe.request_reset({"A"})
        for n in range(4, 6):
            pipe.offer(_raw("B", n))
        gate.set()
        await _wait_until(lambda: ("B", 5) in order)
        st = pipe.stats()
        pipe.stop()
        return order, reset_calls, st

    order, reset_calls, st = asyncio.run(main())
    assert [n for s, n in order if s == "B"] == [0, 1, 2, 3, 4, 5]
    assert reset_calls[0][1] == {"A"}
    assert st["stale_drained"] == 0
    assert st["offered"] == st["committed"]


def test_pre_reset_events_of_reset_symbol_commit_before_reset_like_single_loop():
    async def main() -> tuple[list, object]:
        order: list = []
        reset_calls: list = []
        cleared: list = []
        view = CommittedMDView()
        pipe, gate = _gated_pipeline(order, reset_calls, cleared)

        def commit(r: tuple) -> None:
            view.publish(r[0], r)
            order.append((r[0], r[1]["n"]))

        pipe._commit = commit
        pipe._on_reset_committed = lambda syms: (view.clear(syms), order.append(("cleared", syms)))
        pipe.start(asyncio.get_running_loop())
        for n in range(3):
            pipe.offer(_raw("A", n))
        assert pipe.request_reset({"A"})
        pipe.offer(_raw("A", 3))
        gate.set()
        await _wait_until(lambda: ("A", 3) in order)
        snap = view.snapshot("A")
        pipe.stop()
        return order, snap

    order, snap = asyncio.run(main())
    # Commit order on the loop matches the single-loop path: events processed before the reset are
    # recorded/published, then the reset takes effect (view cleared), then later events.
    committed = [x for x in order if x[0] != "reset"]
    assert committed == [("A", 0), ("A", 1), ("A", 2), ("cleared", frozenset({"A"})), ("A", 3)]
    assert snap is not None and snap[1]["n"] == 3


def test_reconnect_drain_drops_queued_inputs_as_stale_not_overflow():
    pipe = ShardedMDPipeline(2, 100, 256, _compute, lambda r: None)
    pipe._admitting = True
    for n in range(7):
        pipe.offer(_raw(SYMBOLS[n], n))
    assert pipe.drain_stale_inputs() == 7
    st = pipe.stats()
    assert st["stale_drained"] == 7 and st["queued"] == 0
    assert pipe.drop_totals() == (0, 0)


def test_reset_request_refused_when_pipeline_not_running():
    pipe = ShardedMDPipeline(2, 10, 256, _compute, lambda r: None)
    assert pipe.request_reset(None) is False


def test_pool_warmup_reset_routes_to_md_workers_not_loop():
    from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

    pool = QuoteConnectionPool.__new__(QuoteConnectionPool)
    pool._slots = [SimpleNamespace(conn_id="c1", symbols={"A", "B"})]
    pool._lob = MagicMock()
    pool._feature_engine = MagicMock()
    routed: list[set] = []
    pool.set_reset_router(lambda syms: routed.append(syms) or True)
    pool._notify_warmup_reset("c1")
    assert routed == [{"A", "B"}]
    pool._lob.reset_books_for_symbols.assert_not_called()
    pool._feature_engine.reset_symbols.assert_not_called()


def test_post_reconnect_resets_route_to_workers_when_sharded(monkeypatch):
    svc = _svc(monkeypatch, "2")
    svc.lob = MagicMock()

    async def main() -> int:
        svc.loop = asyncio.get_running_loop()
        svc._md_shards.start(svc.loop)
        svc._md_shards._workers[0].seen.add("A")
        svc._apply_post_reconnect_resets()
        await _wait_until(lambda: all(w.resets_applied == 1 for w in svc._md_shards._workers))
        svc._md_shards.stop()
        return svc.lob.reset_books.call_count

    assert asyncio.run(main()) == 0
    svc.lob.reset_books_for_symbols.assert_called_once_with({"A"})


def test_trigger_reconnect_not_connected_when_reset_cannot_be_applied(monkeypatch):
    from hft_platform.services._md_ingestion import FeedState

    client = SimpleNamespace(reconnect=lambda *a: True)
    svc = _svc(monkeypatch, "2", client=client)
    svc._within_reconnect_window = lambda: True
    svc._last_reconnect_ts = 0.0
    # Pipeline never started: the reset cannot reach any worker.
    ok = asyncio.run(svc._trigger_reconnect(120.0, reason="test"))
    assert ok is False
    assert svc.state == FeedState.DISCONNECTED


# -- worker isolation ---------------------------------------------------------------


def test_worker_storm_guard_proxy_does_not_replace_service_guard(monkeypatch):
    guard = MagicMock()
    svc = _svc(monkeypatch, "2", storm_guard=guard)
    assert svc._storm_guard is guard
    assert svc._md_sg_proxy is not guard


def test_worker_normalize_failure_counts_per_worker_not_on_service(monkeypatch):
    svc = _svc(monkeypatch, "2")
    svc.normalizer = MagicMock()
    svc.normalizer.normalize_tick.side_effect = ValueError("bad")
    w = svc._md_shards._workers[0]
    assert svc._md_shard_compute({"code": "A", "close": 1.0}, w) is None
    assert w.norm_failures == 1
    assert svc._norm_consecutive_failures == 0


def test_worker_compute_leaves_first_event_flags_to_commit(monkeypatch):
    from hft_platform.events import MetaData, TickEvent

    svc = _svc(monkeypatch, "1")
    ev = TickEvent(meta=MetaData(seq=1, source_ts=1, local_ts=1), symbol="A", price=10_000, volume=1)
    svc.normalizer = MagicMock()
    svc.normalizer.normalize_tick.return_value = ev
    w = svc._md_shards._workers[0]
    result = svc._md_shard_compute({"code": "A", "close": 1.0}, w)
    assert result is not None
    assert svc._first_tick_event is False
    svc._md_shard_commit(result)
    assert svc._first_tick_event is True


def test_shard_cache_bounded_and_routing_stable():
    pipe = ShardedMDPipeline(4, 10, 256, _compute, lambda r: None, known_symbols=["K1", "K2"], shard_cache_max=4)
    first = {f"X{i}": pipe.shard_of(f"X{i}") for i in range(100)}
    assert len(pipe._shard_cache) == 4
    assert {s: pipe.shard_of(s) for s in first} == first


# -- service wiring -------------------------------------------------------------------


def test_md_workers_default_zero_keeps_single_loop_path(monkeypatch):
    svc = _svc(monkeypatch, "0")
    assert svc._md_shards is None
    assert svc.committed_view is None


def test_md_workers_crash_fails_service_run_and_halts(monkeypatch):
    storm_guard = MagicMock()
    svc = _svc(monkeypatch, "2", storm_guard=storm_guard)
    svc._connect_sequence = AsyncMock()
    svc._monitor_loop = AsyncMock()
    svc._watchdog_loop = AsyncMock()

    def bad_compute(raw, w):
        raise RuntimeError("worker bug")

    svc._md_shards._compute = bad_compute

    async def main() -> None:
        task = asyncio.create_task(svc.run())
        await _wait_until(lambda: svc._md_shards._loop is not None)
        svc._on_shioaji_event("TSE", {"code": "2330", "close": 1.0})
        await asyncio.wait_for(task, 5.0)

    with pytest.raises(MDWorkerCrashedError):
        asyncio.run(main())
    assert storm_guard.trigger_halt.called
    assert svc._md_shards.admitting is False
