"""Sustained committed market-data events/s for HFT_MD_WORKERS N in {0,1,2,4,8} (P3).

N=0 mirrors today's chain: producer thread -> call_soon_threadsafe per event -> LOB + feature + commit on
the loop. N>=1 uses ShardedMDPipeline: producer thread offers, workers run LOB + feature + L1 snapshot,
the loop commits in budgeted batches. Normalization is excluded (events are pre-built BidAskEvents).

Usage: PYTHONPATH=src python scripts/latency/md_sharded_bench.py [--events 60000] [--symbols 32]
Prints one JSON object (includes python version and gil_enabled).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time

import numpy as np

from hft_platform.events import BidAskEvent, MetaData
from hft_platform.feed_adapter.lob_engine import LOBEngine
from hft_platform.services._md_sharded import CommittedMDView, ShardedMDPipeline

TARGET_EV_S = 11_500


def _build_events(n_events: int, n_symbols: int) -> list[BidAskEvent]:
    symbols = [f"BENCH{i:03d}" for i in range(n_symbols)]
    events = []
    for i in range(n_events):
        sym = symbols[i % n_symbols]
        base = 200_000_000 + (i % 50) * 10_000  # x10000 scaled ints
        bids = np.array([[base - k * 10_000, 5 + k] for k in range(5)], dtype=np.int64)
        asks = np.array([[base + (k + 1) * 10_000, 5 + k] for k in range(5)], dtype=np.int64)
        meta = MetaData(seq=i, source_ts=i, local_ts=i, topic="bidask")
        events.append(BidAskEvent(meta=meta, symbol=sym, bids=bids, asks=asks, is_snapshot=True))
    return events


def _make_compute(lob: LOBEngine):
    fe = getattr(lob, "feature_engine", None)

    def compute(event: BidAskEvent, w: object = None) -> tuple:
        stats = lob.process_event(event)
        sym = event.symbol
        feats = fe.get_feature_tuple(sym) if fe else None
        return (sym, event, stats, None, lob.get_l1_scaled(sym), feats, None, getattr(w, "epoch", 0))

    return compute


async def _run_inline(events: list[BidAskEvent]) -> float:
    loop = asyncio.get_running_loop()
    lob = LOBEngine()
    compute = _make_compute(lob)
    view = CommittedMDView()
    done = asyncio.Event()
    count = [0]
    total = len(events)

    def handle(ev: BidAskEvent) -> None:
        r = compute(ev)
        view.publish(r[0], r)
        count[0] += 1
        if count[0] == total:
            done.set()

    def produce() -> None:
        for ev in events:
            loop.call_soon_threadsafe(handle, ev)

    t0 = time.perf_counter()
    th = threading.Thread(target=produce)
    th.start()
    await done.wait()
    elapsed = time.perf_counter() - t0
    th.join()
    return total / elapsed


async def _run_sharded(events: list[BidAskEvent], n_workers: int) -> tuple[float, dict]:
    loop = asyncio.get_running_loop()
    lob = LOBEngine()
    view = CommittedMDView()
    total = len(events)
    done = asyncio.Event()

    def commit(r: tuple) -> None:
        view.publish(r[0], r)

    pipe = ShardedMDPipeline(
        n_workers,
        total,
        256,
        _make_compute(lob),
        commit,
        symbol_of=lambda ev: ev.symbol,
    )
    pipe.start(loop)

    def produce() -> None:
        for ev in events:
            pipe.offer(ev)

    async def watch() -> None:
        while pipe.committed + pipe.drop_totals()[0] + sum(w.rejected for w in pipe._workers) < total:
            await asyncio.sleep(0.001)
        done.set()

    t0 = time.perf_counter()
    th = threading.Thread(target=produce)
    th.start()
    await watch()
    elapsed = time.perf_counter() - t0
    th.join()
    pipe.stop()
    return pipe.committed / elapsed, pipe.stats()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=60_000)
    ap.add_argument("--symbols", type=int, default=32)
    ap.add_argument("--workers", default="0,1,2,4,8")
    args = ap.parse_args()
    events = _build_events(args.events, args.symbols)
    results = []
    for n in (int(x) for x in args.workers.split(",")):
        if n == 0:
            ev_s = asyncio.run(_run_inline(events))
            stats: dict = {}
        else:
            ev_s, stats = asyncio.run(_run_sharded(events, n))
        results.append({"workers": n, "committed_ev_s": round(ev_s, 1), "meets_target": ev_s >= TARGET_EV_S, **stats})
    gil = sys._is_gil_enabled() if hasattr(sys, "_is_gil_enabled") else True
    print(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "gil_enabled": gil,
                "events": args.events,
                "symbols": args.symbols,
                "target_ev_s": TARGET_EV_S,
                "results": results,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
