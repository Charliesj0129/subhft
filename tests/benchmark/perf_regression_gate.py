from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from unittest.mock import MagicMock, patch

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.events import BidAskEvent, MetaData, TickEvent
from hft_platform.feed_adapter import shioaji_client as shio_mod
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.feed_adapter.shioaji_client import ShioajiClient
from hft_platform.recorder.batcher import Batcher
from hft_platform.recorder.mapper import map_event_to_record
from hft_platform.recorder.wal import WALWriter
from hft_platform.recorder.worker import MARKET_DATA_COLUMNS, _extract_market_data
from hft_platform.recorder.writer import DataWriter
from hft_platform.services.market_data import MarketDataService
from hft_platform.strategy.base import BaseStrategy
from hft_platform.strategy.runner import StrategyRunner
from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
from research.backtest.hbt_runner import BacktestConfig, ResearchBacktestRunner
from research.combinatorial.search_engine import AlphaSearchEngine
from research.registry.schemas import AlphaManifest


class _NoopStrategy(BaseStrategy):
    def on_tick(self, event):  # pragma: no cover - microbench hook
        return None


class _DummyAlpha:
    def __init__(self) -> None:
        self.prev = 0.0
        self.manifest = AlphaManifest(
            alpha_id="perf_dummy",
            hypothesis="perf",
            formula="dummy",
            paper_refs=(),
            data_fields=("ofi", "qty"),
            complexity="O(n)",
        )

    def reset(self) -> None:
        self.prev = 0.0

    def update(self, **kwargs) -> float:
        v = float(kwargs.get("ofi", 0.0))
        q = float(kwargs.get("qty", 1.0))
        self.prev = 0.7 * self.prev + (v / max(1.0, q))
        return self.prev

    def get_signal(self) -> float:
        return self.prev


class _RouteClient:
    __slots__ = ("code", "count", "allow_symbol_fallback")

    def __init__(self, code: str) -> None:
        self.code = code
        self.count = 0
        self.allow_symbol_fallback = False

    def _enqueue_tick(self, *args, **kwargs) -> None:
        self.count += 1


class _DummyLoop:
    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0

    def call_soon_threadsafe(self, fn, *args) -> None:  # pragma: no cover - microbench hook
        self.count += 1


async def _run_strategy_loop(runner: StrategyRunner, event: TickEvent, n: int) -> None:
    for _ in range(n):
        await runner.process_event(event)


async def _run_recorder_batcher_add(n: int) -> None:
    batcher = Batcher(
        "hft.market_data",
        flush_limit=n + 1,
        flush_interval_ms=60_000,
        writer=None,
        max_buffer_size=n + 1,
        extractor=_extract_market_data,
        extractor_columns=MARKET_DATA_COLUMNS,
        memory_guard=None,
    )
    row = {
        "symbol": "2330",
        "exchange": "TSE",
        "type": "BidAsk",
        "exch_ts": 1,
        "ingest_ts": 1,
        "price_scaled": 0,
        "volume": 0,
        "bids_price": [100100000, 100000000],
        "bids_vol": [10, 20],
        "asks_price": [100200000, 100300000],
        "asks_vol": [15, 25],
        "seq_no": 1,
    }
    for i in range(n):
        row["seq_no"] = i
        row["exch_ts"] = i
        row["ingest_ts"] = i
        await batcher.add(row)


def bench_strategy_noop(n: int = 50_000) -> float:
    risk_q = asyncio.Queue()
    with patch("hft_platform.strategy.runner.StrategyRegistry") as mock_reg:
        mock_reg.return_value.instantiate.return_value = []
        runner = StrategyRunner(MagicMock(), risk_q, config_path="dummy")
    runner.register(_NoopStrategy("noop", symbols=["2330"]))
    evt = TickEvent(
        meta=MetaData(seq=1, topic="tick", source_ts=1, local_ts=1),
        symbol="2330",
        price=10000,
        volume=1,
        total_volume=1,
        bid_side_total_vol=0,
        ask_side_total_vol=0,
        is_simtrade=False,
        is_odd_lot=False,
    )
    t0 = time.perf_counter()
    asyncio.run(_run_strategy_loop(runner, evt, n))
    t1 = time.perf_counter()
    return (t1 - t0) / n * 1e6


def bench_recorder_batcher_add(n: int = 20_000) -> float:
    t0 = time.perf_counter()
    asyncio.run(_run_recorder_batcher_add(n))
    t1 = time.perf_counter()
    return (t1 - t0) / n * 1e6


def _recorder_bench_metadata() -> tuple[SymbolMetadata, PriceCodec]:
    with TemporaryDirectory() as td:
        cfg = Path(td) / "symbols.yaml"
        cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
        meta = SymbolMetadata(str(cfg))
        codec = PriceCodec(SymbolMetadataPriceScaleProvider(meta))
        return meta, codec


def bench_recorder_map_tick(n: int = 50_000) -> float:
    metadata, codec = _recorder_bench_metadata()
    evt = TickEvent(
        meta=MetaData(seq=1, topic="tick", source_ts=1, local_ts=1),
        symbol="2330",
        price=10000,
        volume=1,
        total_volume=1,
        bid_side_total_vol=0,
        ask_side_total_vol=0,
        is_simtrade=False,
        is_odd_lot=False,
    )
    t0 = time.perf_counter()
    for _ in range(n):
        map_event_to_record(evt, metadata, codec)
    t1 = time.perf_counter()
    return (t1 - t0) / n * 1e6


def bench_recorder_map_bidask(n: int = 20_000) -> float:
    metadata, codec = _recorder_bench_metadata()
    evt = BidAskEvent(
        meta=MetaData(seq=1, topic="bidask", source_ts=1, local_ts=1),
        symbol="2330",
        bids=[[10000, 10], [9990, 20], [9980, 30], [9970, 40], [9960, 50]],
        asks=[[10010, 10], [10020, 20], [10030, 30], [10040, 40], [10050, 50]],
        is_snapshot=False,
    )
    t0 = time.perf_counter()
    for _ in range(n):
        map_event_to_record(evt, metadata, codec)
    t1 = time.perf_counter()
    return (t1 - t0) / n * 1e6


async def _run_real_ck_columnar_once(rows: int = 2000) -> float:
    host = os.getenv("HFT_CLICKHOUSE_HOST", os.getenv("CLICKHOUSE_HOST", "localhost"))
    port = int(os.getenv("HFT_CLICKHOUSE_PORT", os.getenv("CLICKHOUSE_PORT", "8123")))
    with TemporaryDirectory() as td:
        os.environ.setdefault("HFT_CLICKHOUSE_ENABLED", "1")
        writer = DataWriter(ch_host=host, ch_port=port, wal_dir=td)
        try:
            t0 = time.perf_counter()
            await writer.connect_async()
            t1 = time.perf_counter()
            # connect_async includes schema init; benchmark insert separately.
            _ = t1 - t0
            symbols = [f"CK_PERF_{i%8}" for i in range(rows)]
            now_ns = time.time_ns()
            cols = [
                "symbol",
                "exchange",
                "type",
                "exch_ts",
                "ingest_ts",
                "price_scaled",
                "volume",
                "bids_price",
                "bids_vol",
                "asks_price",
                "asks_vol",
                "seq_no",
            ]
            data = [
                symbols,
                ["TSE"] * rows,
                ["Tick"] * rows,
                [now_ns + i for i in range(rows)],
                [now_ns + i for i in range(rows)],
                [1_000_000 + i for i in range(rows)],
                [1] * rows,
                [[] for _ in range(rows)],
                [[] for _ in range(rows)],
                [[] for _ in range(rows)],
                [[] for _ in range(rows)],
                list(range(rows)),
            ]
            t0 = time.perf_counter()
            await writer.write_columnar("hft.market_data", cols, data, rows)
            t1 = time.perf_counter()
        finally:
            await writer.shutdown()
    return (t1 - t0) * 1e3


def bench_recorder_ck_columnar_write_ms(rows: int = 2000) -> float:
    return asyncio.run(_run_real_ck_columnar_once(rows))


def bench_recorder_wal_atomic_stress_ratio(samples: int = 8, rows_per_file: int = 128) -> float:
    payload = [{"seq": i, "symbol": "2330", "p": 1_000_000, "v": 1} for i in range(rows_per_file)]
    with TemporaryDirectory() as td:
        writer = WALWriter(td)
        file_idx = 0

        def _measure_avg(n_files: int) -> float:
            nonlocal file_idx
            durs = []
            for _ in range(n_files):
                file_idx += 1
                fname = str(Path(td) / f"hft.market_data_{time.time_ns()}_{file_idx}.jsonl")
                t0 = time.perf_counter()
                writer._write_sync_atomic(fname, payload)
                durs.append((time.perf_counter() - t0) * 1e3)
            return sum(durs) / max(1, len(durs))

        baseline = _measure_avg(samples)
        stop = threading.Event()

        def _fsync_hammer() -> None:
            block = b"x" * 1024 * 1024
            path = Path(td) / "io_stress.bin"
            with path.open("wb", buffering=0) as f:
                while not stop.is_set():
                    f.write(block)
                    f.flush()
                    os.fsync(f.fileno())

        t = threading.Thread(target=_fsync_hammer, daemon=True)
        t.start()
        try:
            stressed = _measure_avg(samples)
        finally:
            stop.set()
            t.join(timeout=2)
        if baseline <= 0:
            return 1.0
        return stressed / baseline


def bench_research_backtest(rows: int = 40_000) -> float:
    rng = np.random.default_rng(7)
    arr = np.zeros(rows, dtype=[("mid", "f8"), ("ofi", "f8"), ("qty", "i8")])
    arr["mid"] = 100 + np.cumsum(rng.normal(0, 0.02, size=rows))
    arr["ofi"] = rng.normal(0, 1.0, size=rows)
    arr["qty"] = rng.integers(1, 50, size=rows)
    with NamedTemporaryFile(suffix=".npz") as fp:
        np.savez(fp.name, data=arr)
        runner = ResearchBacktestRunner(_DummyAlpha(), BacktestConfig(data_paths=[fp.name]))
        t0 = time.perf_counter()
        runner.run()
        t1 = time.perf_counter()
    return (t1 - t0) / rows * 1e6


def bench_research_search(trials: int = 80) -> float:
    rng = np.random.default_rng(123)
    n = 10_000
    features = {f"f{i}": rng.normal(size=n).astype(np.float64) for i in range(16)}
    returns = rng.normal(scale=0.001, size=n).astype(np.float64)
    pool = {f"p{i}": rng.normal(size=n).astype(np.float64) for i in range(16)}
    engine = AlphaSearchEngine(features=features, returns=returns, pool_signals=pool, random_seed=7)
    t0 = time.perf_counter()
    engine.random_search(n_trials=trials)
    t1 = time.perf_counter()
    return (t1 - t0) / trials * 1e3


def bench_shioaji_callback_dispatch(iters: int = 100_000, clients: int = 64) -> float:
    with shio_mod.CLIENT_REGISTRY_LOCK:
        shio_mod.CLIENT_REGISTRY.clear()
        shio_mod.CLIENT_REGISTRY_BY_CODE.clear()
        shio_mod.CLIENT_REGISTRY_SNAPSHOT = ()
        shio_mod.CLIENT_REGISTRY_BY_CODE_SNAPSHOT = {}
        shio_mod.TOPIC_CODE_CACHE.clear()
    local_clients = []
    for i in range(clients):
        code = f"{2300 + i}"
        c = _RouteClient(code)
        local_clients.append(c)
        shio_mod._registry_register(c)
        shio_mod._registry_rebind_codes(c, [code])
    target_code = local_clients[clients // 2].code
    quote = type("Quote", (), {"code": target_code})()
    topic = f"Q/TSE/{target_code}"
    for _ in range(2000):
        shio_mod.dispatch_tick_cb(topic, quote)
    t0 = time.perf_counter()
    for _ in range(iters):
        shio_mod.dispatch_tick_cb(topic, quote)
    t1 = time.perf_counter()
    return (t1 - t0) / iters * 1e6


def bench_market_data_callback_parse(iters: int = 100_000) -> float:
    raw_q: asyncio.Queue = asyncio.Queue(maxsize=1024)
    client = MagicMock(spec=ShioajiClient)
    client.symbols = [{"code": "2330", "exchange": "TSE", "price_scale": 1}]
    svc = MarketDataService(RingBufferBus(size=1024), raw_q, client)
    svc.loop = _DummyLoop()
    svc._raw_first_seen = True
    svc._raw_first_parsed = True
    payload = {"code": "2330", "ts": 1, "bid_price": [100.0], "bid_volume": [1], "ask_price": [100.1], "ask_volume": [1]}
    for _ in range(2000):
        svc._on_shioaji_event("TSE", payload)
    t0 = time.perf_counter()
    for _ in range(iters):
        svc._on_shioaji_event("TSE", payload)
    t1 = time.perf_counter()
    return (t1 - t0) / iters * 1e6


def _bench_shioaji_callback_soak_metrics(
    iters: int = 120_000,
    clients: int = 64,
    miss_rate: float = 0.02,
) -> tuple[float, float]:
    with shio_mod.CLIENT_REGISTRY_LOCK:
        shio_mod.CLIENT_REGISTRY.clear()
        shio_mod.CLIENT_REGISTRY_BY_CODE.clear()
        shio_mod.CLIENT_REGISTRY_SNAPSHOT = ()
        shio_mod.CLIENT_REGISTRY_BY_CODE_SNAPSHOT = {}
        if hasattr(shio_mod, "CLIENT_REGISTRY_WILDCARD_SNAPSHOT"):
            shio_mod.CLIENT_REGISTRY_WILDCARD_SNAPSHOT = ()
        if hasattr(shio_mod, "CLIENT_DISPATCH_SNAPSHOT"):
            shio_mod.CLIENT_DISPATCH_SNAPSHOT = ()
        if hasattr(shio_mod, "CLIENT_DISPATCH_BY_CODE_SNAPSHOT"):
            shio_mod.CLIENT_DISPATCH_BY_CODE_SNAPSHOT = {}
        if hasattr(shio_mod, "CLIENT_DISPATCH_WILDCARD_SNAPSHOT"):
            shio_mod.CLIENT_DISPATCH_WILDCARD_SNAPSHOT = ()
        shio_mod.TOPIC_CODE_CACHE.clear()

    local_clients = []
    for i in range(clients):
        code = f"{2400 + i}"
        c = _RouteClient(code)
        local_clients.append(c)
        shio_mod._registry_register(c)
        shio_mod._registry_rebind_codes(c, [code])
    # One wildcard fallback client to keep fallback path exercised but bounded.
    wildcard = _RouteClient("wild")
    wildcard.allow_symbol_fallback = True  # type: ignore[attr-defined]
    shio_mod._registry_register(wildcard)

    target_code = local_clients[clients // 2].code
    good_quote = type("Quote", (), {"code": target_code})()
    bad_quote = object()
    good_topic = f"Q/TSE/{target_code}"
    bad_topic = "UNPARSEABLE@@@"

    old_strict = getattr(shio_mod, "_ROUTE_MISS_STRICT", False)
    old_log_every = getattr(shio_mod, "_ROUTE_MISS_LOG_EVERY", 100)
    old_fallback_mode = getattr(shio_mod, "_ROUTE_MISS_FALLBACK_MODE", "wildcard")
    shio_mod._ROUTE_MISS_STRICT = False
    shio_mod._ROUTE_MISS_FALLBACK_MODE = "wildcard"
    shio_mod._ROUTE_MISS_LOG_EVERY = 10_000_000
    try:
        warm = 2000
        every = max(1, int(1.0 / max(miss_rate, 1e-9)))
        for i in range(warm):
            if miss_rate > 0 and (i % every == 0):
                shio_mod.dispatch_tick_cb(bad_topic, bad_quote)
            else:
                shio_mod.dispatch_tick_cb(good_topic, good_quote)
        for c in local_clients:
            c.count = 0
        wildcard.count = 0
        t0 = time.perf_counter()
        for i in range(iters):
            if miss_rate > 0 and (i % every == 0):
                shio_mod.dispatch_tick_cb(bad_topic, bad_quote)
            else:
                shio_mod.dispatch_tick_cb(good_topic, good_quote)
        t1 = time.perf_counter()
    finally:
        shio_mod._ROUTE_MISS_STRICT = old_strict
        shio_mod._ROUTE_MISS_FALLBACK_MODE = old_fallback_mode
        shio_mod._ROUTE_MISS_LOG_EVERY = old_log_every

    total_calls = sum(c.count for c in local_clients) + wildcard.count
    dispatches = iters
    us_per_dispatch = (t1 - t0) / max(dispatches, 1) * 1e6
    calls_per_dispatch = total_calls / max(dispatches, 1)
    return us_per_dispatch, calls_per_dispatch


def bench_shioaji_callback_soak_us_per_dispatch() -> float:
    return _bench_shioaji_callback_soak_metrics()[0]


def bench_shioaji_callback_soak_calls_per_dispatch() -> float:
    return _bench_shioaji_callback_soak_metrics()[1]


def _multi_sample(bench_fn, n_runs: int = 3) -> tuple[float, float]:
    """Run bench_fn n_runs times; return (mean, std) for variance-buffered comparison."""
    samples = [bench_fn() for _ in range(n_runs)]
    mean = sum(samples) / len(samples)
    variance = sum((s - mean) ** 2 for s in samples) / max(1, len(samples) - 1)
    std = variance ** 0.5
    return mean, std


def main() -> int:
    parser = argparse.ArgumentParser(description="Lightweight perf regression gate for strategy/research/feed/recorder hot paths.")
    parser.add_argument("--baseline", default="tests/benchmark/perf_baselines.json")
    parser.add_argument("--json", default="")
    parser.add_argument(
        "--sigma",
        type=float,
        default=3.0,
        help="Number of standard deviations of variance buffer added to limit (default: 3.0)",
    )
    parser.add_argument("--runs", type=int, default=3, help="Samples per benchmark for variance estimation")
    parser.add_argument(
        "--include-recorder-io",
        action="store_true",
        help="Run heavier recorder I/O drills (real ClickHouse + WAL stress). Intended for nightly/manual runs.",
    )
    args = parser.parse_args()

    baseline = json.loads(Path(args.baseline).read_text())
    sigma_buf = float(args.sigma)
    n_runs = max(1, int(args.runs))

    noop_mean, noop_std = _multi_sample(bench_strategy_noop, n_runs)
    bt_mean, bt_std = _multi_sample(bench_research_backtest, n_runs)
    search_mean, search_std = _multi_sample(bench_research_search, n_runs)
    cb_dispatch_mean, cb_dispatch_std = _multi_sample(bench_shioaji_callback_dispatch, n_runs)
    cb_parse_mean, cb_parse_std = _multi_sample(bench_market_data_callback_parse, n_runs)
    cb_soak_us_mean, cb_soak_us_std = _multi_sample(bench_shioaji_callback_soak_us_per_dispatch, n_runs)
    cb_soak_calls_mean, cb_soak_calls_std = _multi_sample(bench_shioaji_callback_soak_calls_per_dispatch, n_runs)
    rec_batcher_mean, rec_batcher_std = _multi_sample(bench_recorder_batcher_add, n_runs)
    rec_tick_mean, rec_tick_std = _multi_sample(bench_recorder_map_tick, n_runs)
    rec_bidask_mean, rec_bidask_std = _multi_sample(bench_recorder_map_bidask, n_runs)
    include_recorder_io = bool(args.include_recorder_io or os.getenv("HFT_PERF_GATE_RECORDER_IO", "0") in {"1", "true", "yes", "on"})
    if include_recorder_io:
        rec_ck_mean, rec_ck_std = _multi_sample(bench_recorder_ck_columnar_write_ms, max(1, min(n_runs, 2)))
        rec_wal_stress_mean, rec_wal_stress_std = _multi_sample(bench_recorder_wal_atomic_stress_ratio, max(1, min(n_runs, 2)))
    else:
        rec_ck_mean = rec_ck_std = rec_wal_stress_mean = rec_wal_stress_std = None

    results = {
        "strategy_noop_metrics_on_us_per_event": noop_mean,
        "research_backtest_us_per_row": bt_mean,
        "research_search_ms_per_trial": search_mean,
        "shioaji_callback_dispatch_us_per_event": cb_dispatch_mean,
        "market_data_callback_parse_us_per_event": cb_parse_mean,
        "shioaji_callback_soak_us_per_dispatch": cb_soak_us_mean,
        "shioaji_callback_soak_calls_per_dispatch": cb_soak_calls_mean,
        "recorder_batcher_add_us_per_row": rec_batcher_mean,
        "recorder_map_tick_us_per_event": rec_tick_mean,
        "recorder_map_bidask_us_per_event": rec_bidask_mean,
    }
    if include_recorder_io:
        results["recorder_ck_columnar_write_ms"] = rec_ck_mean
        results["recorder_wal_atomic_stress_ratio"] = rec_wal_stress_mean
    stdevs = {
        "strategy_noop_metrics_on_us_per_event": noop_std,
        "research_backtest_us_per_row": bt_std,
        "research_search_ms_per_trial": search_std,
        "shioaji_callback_dispatch_us_per_event": cb_dispatch_std,
        "market_data_callback_parse_us_per_event": cb_parse_std,
        "shioaji_callback_soak_us_per_dispatch": cb_soak_us_std,
        "shioaji_callback_soak_calls_per_dispatch": cb_soak_calls_std,
        "recorder_batcher_add_us_per_row": rec_batcher_std,
        "recorder_map_tick_us_per_event": rec_tick_std,
        "recorder_map_bidask_us_per_event": rec_bidask_std,
    }
    if include_recorder_io:
        stdevs["recorder_ck_columnar_write_ms"] = rec_ck_std
        stdevs["recorder_wal_atomic_stress_ratio"] = rec_wal_stress_std

    checks = [
        (
            "strategy_noop_metrics_on_us_per_event",
            "strategy_noop_metrics_on_us_per_event_max",
        ),
        (
            "research_backtest_us_per_row",
            "research_backtest_us_per_row_max",
        ),
        (
            "research_search_ms_per_trial",
            "research_search_ms_per_trial_max",
        ),
        (
            "shioaji_callback_dispatch_us_per_event",
            "shioaji_callback_dispatch_us_per_event_max",
        ),
        (
            "market_data_callback_parse_us_per_event",
            "market_data_callback_parse_us_per_event_max",
        ),
        (
            "shioaji_callback_soak_us_per_dispatch",
            "shioaji_callback_soak_us_per_dispatch_max",
        ),
        (
            "shioaji_callback_soak_calls_per_dispatch",
            "shioaji_callback_soak_calls_per_dispatch_max",
        ),
        (
            "recorder_batcher_add_us_per_row",
            "recorder_batcher_add_us_per_row_max",
        ),
        (
            "recorder_map_tick_us_per_event",
            "recorder_map_tick_us_per_event_max",
        ),
        (
            "recorder_map_bidask_us_per_event",
            "recorder_map_bidask_us_per_event_max",
        ),
    ]
    if include_recorder_io:
        checks.extend(
            [
                ("recorder_ck_columnar_write_ms", "recorder_ck_columnar_write_ms_max"),
                ("recorder_wal_atomic_stress_ratio", "recorder_wal_atomic_stress_ratio_max"),
            ]
        )
    failures: list[str] = []
    for metric_key, limit_key in checks:
        if metric_key not in results or limit_key not in baseline:
            continue
        limit = float(baseline[limit_key])
        value = float(results[metric_key])
        std = float(stdevs[metric_key])
        # T4: Add 3-sigma variance buffer to avoid flaky failures from measurement noise
        effective_limit = limit + sigma_buf * std
        if value > effective_limit:
            failures.append(
                f"{metric_key}={value:.3f} > {limit:.3f} (limit+{sigma_buf}σ={effective_limit:.3f}, std={std:.3f})"
            )

    payload = {"results": results, "stdevs": stdevs, "baseline": baseline, "failures": failures}
    text = json.dumps(payload, indent=2, sort_keys=True)
    print(text)
    if args.json:
        Path(args.json).write_text(text)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
