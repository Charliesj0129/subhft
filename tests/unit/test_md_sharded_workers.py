"""P3: symbol-sharded market-data worker threads (HFT_MD_WORKERS)."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.services._md_sharded import (
    CommittedMDView,
    MDWorkerCrashedError,
    ShardedMDPipeline,
)

SYMBOLS = [f"S{i:03d}" for i in range(32)]


def _raw(sym: str, n: int) -> dict:
    return {"code": sym, "n": n}


def _compute(raw: dict) -> tuple:
    return (raw["code"], raw, None, None, None, None)


async def _wait_until(pred, timeout_s: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not pred():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.001)


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

        def slow_compute(raw: dict) -> tuple:
            gate.wait()
            return _compute(raw)

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


def test_overflow_drop_reports_consecutive_count_on_full_shard():
    drops: list[int] = []
    pipe = ShardedMDPipeline(1, 3, 256, _compute, lambda r: None, on_drop=drops.append)
    pipe._admitting = True  # admission open, workers not started: the inbox only fills
    accepted = [pipe.offer(_raw("A", n)) for n in range(6)]
    assert accepted == [True, True, True, False, False, False]
    assert drops == [1, 2, 3]
    assert pipe.stats()["dropped"] == 3


def test_worker_crash_notifies_supervisor_and_stops_admission():
    async def main() -> tuple[list, bool, bool]:
        crashes: list = []

        def bad_compute(raw: dict) -> tuple:
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
        pipe_holder: list[ShardedMDPipeline] = []
        gate = threading.Event()

        def gated_compute(raw: dict) -> tuple:
            gate.wait()
            return _compute(raw)

        def commit(r: tuple) -> None:
            if r[1]["n"] == 10:
                # A fill/cancel arriving mid-burst: it must not wait for the whole burst.
                loop.call_soon(lambda: committed_at_fill.append(pipe_holder[0].committed))

        pipe = ShardedMDPipeline(1, 20_000, 256, gated_compute, commit)
        pipe_holder.append(pipe)
        pipe.start(loop)
        for n in range(10_000):
            pipe.offer(_raw("A", n))
        gate.set()
        await _wait_until(lambda: pipe.stats()["queued"] == 0)
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

        def compute(raw: dict) -> tuple:
            n = raw["n"]
            book["bid"] = n * 10
            book["ask"] = n * 10 + 1  # live state is half-applied between these two writes
            l1 = (n, book["bid"], book["ask"], 0, 0, 0, 0)
            return ("A", raw, None, None, l1, (n,))

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


def test_reconnect_reset_runs_with_workers_paused_and_drops_stale_inputs():
    async def main() -> tuple[int, bool, int]:
        gate = threading.Event()
        in_compute = threading.Event()

        def compute(raw: dict) -> tuple:
            in_compute.set()
            gate.wait()
            return _compute(raw)

        pipe = ShardedMDPipeline(1, 100, 256, compute, lambda r: None)
        pipe.start(asyncio.get_running_loop())
        for n in range(10):
            pipe.offer(_raw("A", n))
        await _wait_until(in_compute.is_set)
        ran: list[bool] = []
        gate.set()
        stale = pipe.run_quiesced(lambda: ran.append(True), drain_inputs=True)
        await _wait_until(lambda: pipe.committed >= 1)
        await asyncio.sleep(0.01)
        committed = pipe.committed
        pipe.stop()
        return stale, bool(ran), committed

    stale, ran, committed = asyncio.run(main())
    assert ran is True
    assert stale + committed == 10
    assert committed >= 1


def test_md_workers_default_zero_keeps_single_loop_path(monkeypatch):
    from hft_platform.services.market_data import MarketDataService

    monkeypatch.delenv("HFT_MD_WORKERS", raising=False)
    svc = MarketDataService(MagicMock(), asyncio.Queue(maxsize=16), MagicMock())
    assert svc._md_shards is None
    assert svc.committed_view is None


def test_md_workers_crash_fails_service_run_and_halts(monkeypatch):
    from hft_platform.services.market_data import MarketDataService

    monkeypatch.setenv("HFT_MD_WORKERS", "2")
    storm_guard = MagicMock()
    svc = MarketDataService(MagicMock(), asyncio.Queue(maxsize=16), MagicMock(), storm_guard=storm_guard)
    assert svc._md_shards is not None
    svc._connect_sequence = AsyncMock()
    svc._monitor_loop = AsyncMock()
    svc._watchdog_loop = AsyncMock()

    def bad_compute(raw):
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
