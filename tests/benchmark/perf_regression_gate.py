from __future__ import annotations

import argparse
import asyncio
import contextlib
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
from hft_platform.events import BidAskEvent, LOBStatsEvent, MetaData, TickEvent
from hft_platform.feature.engine import FeatureEngine
from hft_platform.feed_adapter import shioaji_client as shio_mod
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.feed_adapter.shioaji_client import ShioajiClient
from hft_platform.recorder.batcher import Batcher
from hft_platform.recorder.mapper import map_event_to_record
from hft_platform.recorder.wal import WALWriter
from hft_platform.recorder.worker import MARKET_DATA_COLUMNS, _extract_market_data
from hft_platform.recorder.writer import DataWriter
from hft_platform.services.market_data import MarketDataService
from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, RiskDecision, Side, StormGuardState
from hft_platform.gateway.channel import IntentEnvelope, LocalIntentChannel, TypedIntentEnvelope
from hft_platform.gateway.dedup import IdempotencyStore
from hft_platform.gateway.exposure import ExposureStore
from hft_platform.gateway.policy import GatewayPolicy
from hft_platform.gateway.service import GatewayService
from hft_platform.risk.engine import RiskEngine
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


class _NoopAPIQueue:
    __slots__ = ()

    def put_nowait(self, item) -> None:  # pragma: no cover - microbench hook
        return None


class _NoopOrderAdapter:
    __slots__ = ("_api_queue", "_typed_count", "_supports_typed_command_ingress")

    def __init__(self) -> None:
        self._api_queue = _NoopAPIQueue()
        self._typed_count = 0
        self._supports_typed_command_ingress = True

    def submit_typed_command_nowait(self, frame) -> None:  # pragma: no cover - microbench hook
        self._typed_count += 1


class _StormGuardStub:
    __slots__ = ("state",)

    def __init__(self) -> None:
        self.state = StormGuardState.NORMAL


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


def _risk_bench_config_text() -> str:
    return """
global_defaults:
  max_price_cap: 5000.0
  price_band_ticks: 20
  tick_size: 0.01
  max_notional: 10000000
risk:
  max_order_size: 1000
storm_guard:
  warm_threshold: -200000
  storm_threshold: -500000
  halt_threshold: -1000000
strategies:
  s1:
    max_notional: 10000000
    price_band_ticks: 20
"""


def _make_risk_engine_for_bench(tmpdir: str) -> RiskEngine:
    cfg = Path(tmpdir) / "risk_perf.yaml"
    cfg.write_text(_risk_bench_config_text())
    return RiskEngine(str(cfg), asyncio.Queue(), asyncio.Queue())


def _make_risk_intent(intent_id: int = 1, *, price: int = 1_000_000, qty: int = 1, key: str = "") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="s1",
        symbol="2330",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=price,
        qty=qty,
        tif=TIF.LIMIT,
        target_order_id=None,
        timestamp_ns=0,
        source_ts_ns=0,
        reason="",
        trace_id="",
        idempotency_key=key,
        ttl_ns=0,
    )


def _make_typed_frame(intent: OrderIntent):
    return (
        "typed_intent_v1",
        intent.intent_id,
        intent.strategy_id,
        intent.symbol,
        int(intent.intent_type),
        int(intent.side),
        intent.price,
        intent.qty,
        int(intent.tif),
        intent.target_order_id or "",
        intent.timestamp_ns,
        intent.source_ts_ns,
        intent.reason,
        intent.trace_id,
        intent.idempotency_key,
        intent.ttl_ns,
    )


def bench_risk_evaluate(n: int = 50_000) -> float:
    with TemporaryDirectory() as td:
        engine = _make_risk_engine_for_bench(td)
        intent = _make_risk_intent(price=1_000_000, qty=1)
        t0 = time.perf_counter()
        for _ in range(n):
            engine.evaluate(intent)
        t1 = time.perf_counter()
    return (t1 - t0) / n * 1e6


def bench_risk_evaluate_typed_frame(n: int = 50_000) -> float:
    with TemporaryDirectory() as td:
        engine = _make_risk_engine_for_bench(td)
        frame = _make_typed_frame(_make_risk_intent(price=1_000_000, qty=1))
        t0 = time.perf_counter()
        for _ in range(n):
            engine.evaluate_typed_frame(frame)
        t1 = time.perf_counter()
    return (t1 - t0) / n * 1e6


async def _run_risk_engine_once(kind: str, n: int) -> float:
    with TemporaryDirectory() as td:
        engine = _make_risk_engine_for_bench(td)
        if kind == "reject":
            intent = _make_risk_intent(price=0, qty=1)
        else:
            intent = _make_risk_intent(price=1_000_000, qty=1)
        with patch("hft_platform.risk.engine.logger.warning"), patch("hft_platform.risk.engine.logger.info"), patch(
            "hft_platform.risk.engine.logger.exception"
        ):
            task = asyncio.create_task(engine.run())
            await asyncio.sleep(0)
            t0 = time.perf_counter()
            for i in range(n):
                engine.intent_queue.put_nowait(
                    OrderIntent(
                        intent_id=intent.intent_id + i,
                        strategy_id=intent.strategy_id,
                        symbol=intent.symbol,
                        intent_type=intent.intent_type,
                        side=intent.side,
                        price=intent.price,
                        qty=intent.qty,
                        tif=intent.tif,
                        target_order_id=intent.target_order_id,
                        timestamp_ns=intent.timestamp_ns,
                        source_ts_ns=intent.source_ts_ns,
                        reason=intent.reason,
                        trace_id=intent.trace_id,
                        idempotency_key="",
                        ttl_ns=intent.ttl_ns,
                    )
                )
            await engine.intent_queue.join()
            t1 = time.perf_counter()
            engine.running = False
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    return (t1 - t0) / n * 1e6


def bench_risk_run_approve(n: int = 5_000) -> float:
    return asyncio.run(_run_risk_engine_once("approve", n))


def bench_risk_run_reject(n: int = 5_000) -> float:
    return asyncio.run(_run_risk_engine_once("reject", n))


async def _bench_gateway_process_envelope(kind: str, n: int = 10_000) -> float:
    channel = LocalIntentChannel(maxsize=1024, ttl_ms=0)
    with TemporaryDirectory() as td:
        risk_engine = _make_risk_engine_for_bench(td)
        order_adapter = _NoopOrderAdapter()
        svc = GatewayService(
            channel=channel,
            risk_engine=risk_engine,
            order_adapter=order_adapter,
            exposure_store=ExposureStore(),
            dedup_store=IdempotencyStore(persist_enabled=False),
            storm_guard=_StormGuardStub(),
            policy=GatewayPolicy(),
        )
        t0 = time.perf_counter()
        if kind == "typed":
            base = _make_risk_intent(price=1_000_000, qty=1)
            for i in range(n):
                frame = (
                    "typed_intent_v1",
                    i + 1,
                    base.strategy_id,
                    base.symbol,
                    int(base.intent_type),
                    int(base.side),
                    base.price,
                    base.qty,
                    int(base.tif),
                    "",
                    0,
                    0,
                    "",
                    "",
                    "",
                    0,
                )
                env = TypedIntentEnvelope(payload=frame, enqueued_ns=0, ack_token=str(i))
                await svc._process_envelope(env)
        else:
            for i in range(n):
                intent = _make_risk_intent(intent_id=i + 1, price=1_000_000, qty=1, key="")
                env = IntentEnvelope(intent=intent, enqueued_ns=0, ack_token=str(i))
                await svc._process_envelope(env)
        t1 = time.perf_counter()
    return (t1 - t0) / n * 1e6


def bench_gateway_process_envelope_obj(n: int = 10_000) -> float:
    return asyncio.run(_bench_gateway_process_envelope("obj", n))


def bench_gateway_process_envelope_typed(n: int = 10_000) -> float:
    return asyncio.run(_bench_gateway_process_envelope("typed", n))


def bench_risk_typed_frame_view(n: int = 100_000) -> float:
    with TemporaryDirectory() as td:
        engine = _make_risk_engine_for_bench(td)
        frame = _make_typed_frame(_make_risk_intent())
        t0 = time.perf_counter()
        for _ in range(n):
            engine.typed_frame_view(frame)
        t1 = time.perf_counter()
    return (t1 - t0) / n * 1e6


def bench_risk_create_command_from_typed(n: int = 20_000) -> float:
    with TemporaryDirectory() as td:
        engine = _make_risk_engine_for_bench(td)
        frame = _make_typed_frame(_make_risk_intent())
        view = engine.typed_frame_view(frame)
        t0 = time.perf_counter()
        for _ in range(n):
            engine.create_command_from_typed_frame(frame, intent_view=view)
        t1 = time.perf_counter()
    return (t1 - t0) / n * 1e6


def bench_risk_gateway_typed_ratio_heavy() -> float:
    obj = asyncio.run(_bench_gateway_process_envelope("obj", 20_000))
    typed = asyncio.run(_bench_gateway_process_envelope("typed", 20_000))
    if obj <= 0:
        return 1.0
    return typed / obj


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


def _feature_bench_event_stats(seq: int, bid_px: int, bid_qty: int, ask_px: int, ask_qty: int) -> tuple[BidAskEvent, LOBStatsEvent]:
    evt = BidAskEvent(
        meta=MetaData(seq=seq, topic="bidask", source_ts=seq, local_ts=seq),
        symbol="2330",
        bids=np.asarray([[bid_px, bid_qty]], dtype=np.int64),
        asks=np.asarray([[ask_px, ask_qty]], dtype=np.int64),
        is_snapshot=False,
    )
    stats = LOBStatsEvent(
        symbol="2330",
        ts=seq,
        imbalance=0.0,
        best_bid=bid_px,
        best_ask=ask_px,
        bid_depth=bid_qty,
        ask_depth=ask_qty,
    )
    return evt, stats


def bench_feature_engine_lob_stats(iters: int = 80_000) -> float:
    eng = FeatureEngine(emit_events=True)
    _evt, stats = _feature_bench_event_stats(1, 1_000_000, 10, 1_001_000, 12)
    for i in range(2000):
        stats.ts = i + 1
        eng.process_lob_stats(stats, local_ts_ns=i + 1)
    t0 = time.perf_counter()
    for i in range(iters):
        stats.ts = i + 10_000
        eng.process_lob_stats(stats, local_ts_ns=i + 10_000)
    t1 = time.perf_counter()
    return (t1 - t0) / iters * 1e6


def bench_feature_engine_lob_update(iters: int = 80_000) -> float:
    eng = FeatureEngine(emit_events=True)
    events: list[tuple[BidAskEvent, LOBStatsEvent]] = []
    bid = 1_000_000
    ask = 1_001_000
    bid_qty = 10
    ask_qty = 12
    for i in range(32):
        if i % 3 == 0:
            bid += 100
        if i % 5 == 0:
            ask += 100
        bid_qty = max(1, bid_qty + ((i % 4) - 1))
        ask_qty = max(1, ask_qty + (1 - (i % 3)))
        events.append(_feature_bench_event_stats(i + 1, bid, bid_qty, ask, ask_qty))
    for i in range(2000):
        evt, stats = events[i % len(events)]
        stats.ts = i + 1
        evt.meta.seq = i + 1
        evt.meta.source_ts = i + 1
        evt.meta.local_ts = i + 1
        eng.process_lob_update(evt, stats, local_ts_ns=i + 1)
    t0 = time.perf_counter()
    for i in range(iters):
        evt, stats = events[i % len(events)]
        ts = i + 10_000
        stats.ts = ts
        evt.meta.seq = ts
        evt.meta.source_ts = ts
        evt.meta.local_ts = ts
        eng.process_lob_update(evt, stats, local_ts_ns=ts)
    t1 = time.perf_counter()
    return (t1 - t0) / iters * 1e6


class _FeatureRefState:
    __slots__ = (
        "prev_best_bid",
        "prev_best_ask",
        "prev_bid_qty",
        "prev_ask_qty",
        "ofi_cum",
        "ofi_ema8",
        "spread_ema8",
        "imb_ema8",
        "initialized",
    )

    def __init__(self) -> None:
        self.prev_best_bid = 0
        self.prev_best_ask = 0
        self.prev_bid_qty = 0
        self.prev_ask_qty = 0
        self.ofi_cum = 0
        self.ofi_ema8 = 0.0
        self.spread_ema8 = 0.0
        self.imb_ema8 = 0.0
        self.initialized = False


def _feature_ref_values(
    st: _FeatureRefState,
    evt: BidAskEvent,
    stats: LOBStatsEvent,
) -> tuple[int, ...]:
    bid = int(stats.best_bid)
    ask = int(stats.best_ask)
    mid_x2 = int(stats.mid_price_x2 or (bid + ask))
    spread = int(stats.spread_scaled or (ask - bid))
    bid_depth = int(stats.bid_depth)
    ask_depth = int(stats.ask_depth)
    bid_qty = int(evt.bids[0][1])
    ask_qty = int(evt.asks[0][1])

    d_total = bid_depth + ask_depth
    imbalance_ppm = int(round(((bid_depth - ask_depth) * 1_000_000.0) / d_total)) if d_total > 0 else 0
    l1_total = bid_qty + ask_qty
    if l1_total > 0:
        l1_imb_ppm = int(round(((bid_qty - ask_qty) * 1_000_000.0) / l1_total))
        microprice_x2 = int(round((2.0 * ((ask * bid_qty) + (bid * ask_qty))) / l1_total))
    else:
        l1_imb_ppm = 0
        microprice_x2 = mid_x2

    if not st.initialized:
        ofi_raw = 0
        ofi_cum = 0
        ofi_ema8 = 0
        st.spread_ema8 = float(spread)
        st.imb_ema8 = float(l1_imb_ppm)
        spread_ema8 = int(round(st.spread_ema8))
        imb_ema8 = int(round(st.imb_ema8))
        st.initialized = True
    else:
        if bid > st.prev_best_bid:
            b_flow = bid_qty
        elif bid == st.prev_best_bid:
            b_flow = bid_qty - st.prev_bid_qty
        else:
            b_flow = -st.prev_bid_qty

        if ask > st.prev_best_ask:
            a_flow = -st.prev_ask_qty
        elif ask == st.prev_best_ask:
            a_flow = ask_qty - st.prev_ask_qty
        else:
            a_flow = ask_qty

        ofi_raw = int(b_flow - a_flow)
        st.ofi_cum += ofi_raw
        alpha = 2.0 / 9.0
        st.ofi_ema8 = (1.0 - alpha) * st.ofi_ema8 + alpha * float(ofi_raw)
        st.spread_ema8 = (1.0 - alpha) * st.spread_ema8 + alpha * float(spread)
        st.imb_ema8 = (1.0 - alpha) * st.imb_ema8 + alpha * float(l1_imb_ppm)
        ofi_cum = int(st.ofi_cum)
        ofi_ema8 = int(round(st.ofi_ema8))
        spread_ema8 = int(round(st.spread_ema8))
        imb_ema8 = int(round(st.imb_ema8))

    st.prev_best_bid = bid
    st.prev_best_ask = ask
    st.prev_bid_qty = bid_qty
    st.prev_ask_qty = ask_qty

    return (
        bid,
        ask,
        mid_x2,
        spread,
        bid_depth,
        ask_depth,
        imbalance_ppm,
        microprice_x2,
        bid_qty,
        ask_qty,
        l1_imb_ppm,
        int(ofi_raw),
        int(ofi_cum),
        int(ofi_ema8),
        int(spread_ema8),
        int(imb_ema8),
    )


def bench_feature_engine_parity_mismatch_rate(n: int = 8_000) -> float:
    rng = np.random.default_rng(20260224)
    eng = FeatureEngine(emit_events=True)
    ref = _FeatureRefState()
    bid = 1_000_000
    ask = 1_001_000
    bq = 10
    aq = 12
    mismatches = 0
    for i in range(n):
        bid += int(rng.choice([-100, 0, 100]))
        ask = max(bid + 100, ask + int(rng.choice([-100, 0, 100])))
        bq = max(1, bq + int(rng.choice([-3, -1, 0, 1, 3])))
        aq = max(1, aq + int(rng.choice([-3, -1, 0, 1, 3])))
        evt, stats = _feature_bench_event_stats(i + 1, bid, bq, ask, aq)
        got = eng.process_lob_update(evt, stats, local_ts_ns=i + 1)
        if got is None:
            mismatches += 1
            continue
        expected = _feature_ref_values(ref, evt, stats)
        if got.values != expected:
            mismatches += 1
    return mismatches / max(1, n)


def bench_feature_engine_lob_update_rust(iters: int = 80_000) -> float:
    eng = FeatureEngine(emit_events=True, kernel_backend="rust")
    if eng.kernel_backend() != "rust":
        return 0.0
    events: list[tuple[BidAskEvent, LOBStatsEvent]] = []
    bid = 1_000_000
    ask = 1_001_000
    bid_qty = 10
    ask_qty = 12
    for i in range(32):
        if i % 3 == 0:
            bid += 100
        if i % 5 == 0:
            ask += 100
        bid_qty = max(1, bid_qty + ((i % 4) - 1))
        ask_qty = max(1, ask_qty + (1 - (i % 3)))
        events.append(_feature_bench_event_stats(i + 1, bid, bid_qty, ask, ask_qty))
    for i in range(2000):
        evt, stats = events[i % len(events)]
        ts = i + 1
        stats.ts = ts
        evt.meta.seq = ts
        evt.meta.source_ts = ts
        evt.meta.local_ts = ts
        eng.process_lob_update(evt, stats, local_ts_ns=ts)
    t0 = time.perf_counter()
    for i in range(iters):
        evt, stats = events[i % len(events)]
        ts = i + 10_000
        stats.ts = ts
        evt.meta.seq = ts
        evt.meta.source_ts = ts
        evt.meta.local_ts = ts
        eng.process_lob_update(evt, stats, local_ts_ns=ts)
    t1 = time.perf_counter()
    return (t1 - t0) / iters * 1e6


def bench_feature_engine_python_vs_rust_parity_mismatch_rate(n: int = 8_000) -> float:
    py_eng = FeatureEngine(emit_events=True, kernel_backend="python")
    rust_eng = FeatureEngine(emit_events=True, kernel_backend="rust")
    if rust_eng.kernel_backend() != "rust":
        return 0.0
    rng = np.random.default_rng(20260224)
    bid = 1_000_000
    ask = 1_001_000
    bq = 10
    aq = 12
    mismatches = 0
    for i in range(n):
        bid += int(rng.choice([-100, 0, 100]))
        ask = max(bid + 100, ask + int(rng.choice([-100, 0, 100])))
        bq = max(1, bq + int(rng.choice([-3, -1, 0, 1, 3])))
        aq = max(1, aq + int(rng.choice([-3, -1, 0, 1, 3])))
        evt, stats = _feature_bench_event_stats(i + 1, bid, bq, ask, aq)
        p = py_eng.process_lob_update(evt, stats, local_ts_ns=i + 1)
        r = rust_eng.process_lob_update(evt, stats, local_ts_ns=i + 1)
        if p is None or r is None or p.values != r.values:
            mismatches += 1
    return mismatches / max(1, n)


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
    parser.add_argument(
        "--include-risk-heavy",
        action="store_true",
        help="Run heavier risk/gateway drills (typed conversion + gateway typed/object ratio). Intended for nightly/manual runs.",
    )
    parser.add_argument(
        "--include-feature-rust",
        action="store_true",
        help="Run FeatureEngine Rust-kernel parity/perf drills (requires rebuilt rust_core extension).",
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
    feature_stats_mean, feature_stats_std = _multi_sample(bench_feature_engine_lob_stats, n_runs)
    feature_l1_mean, feature_l1_std = _multi_sample(bench_feature_engine_lob_update, n_runs)
    feature_parity_mean, feature_parity_std = _multi_sample(bench_feature_engine_parity_mismatch_rate, max(1, min(n_runs, 2)))
    risk_eval_mean, risk_eval_std = _multi_sample(bench_risk_evaluate, n_runs)
    risk_eval_typed_mean, risk_eval_typed_std = _multi_sample(bench_risk_evaluate_typed_frame, n_runs)
    risk_run_approve_mean, risk_run_approve_std = _multi_sample(bench_risk_run_approve, max(1, min(n_runs, 2)))
    risk_run_reject_mean, risk_run_reject_std = _multi_sample(bench_risk_run_reject, max(1, min(n_runs, 2)))
    gw_obj_mean, gw_obj_std = _multi_sample(bench_gateway_process_envelope_obj, max(1, min(n_runs, 2)))
    gw_typed_mean, gw_typed_std = _multi_sample(bench_gateway_process_envelope_typed, max(1, min(n_runs, 2)))
    rec_batcher_mean, rec_batcher_std = _multi_sample(bench_recorder_batcher_add, n_runs)
    rec_tick_mean, rec_tick_std = _multi_sample(bench_recorder_map_tick, n_runs)
    rec_bidask_mean, rec_bidask_std = _multi_sample(bench_recorder_map_bidask, n_runs)
    include_recorder_io = bool(args.include_recorder_io or os.getenv("HFT_PERF_GATE_RECORDER_IO", "0") in {"1", "true", "yes", "on"})
    include_risk_heavy = bool(args.include_risk_heavy or os.getenv("HFT_PERF_GATE_RISK_HEAVY", "0") in {"1", "true", "yes", "on"})
    include_feature_rust = bool(
        args.include_feature_rust or os.getenv("HFT_PERF_GATE_FEATURE_RUST", "0") in {"1", "true", "yes", "on"}
    )
    if include_recorder_io:
        rec_ck_mean, rec_ck_std = _multi_sample(bench_recorder_ck_columnar_write_ms, max(1, min(n_runs, 2)))
        rec_wal_stress_mean, rec_wal_stress_std = _multi_sample(bench_recorder_wal_atomic_stress_ratio, max(1, min(n_runs, 2)))
    else:
        rec_ck_mean = rec_ck_std = rec_wal_stress_mean = rec_wal_stress_std = None
    if include_risk_heavy:
        risk_typed_view_mean, risk_typed_view_std = _multi_sample(bench_risk_typed_frame_view, max(1, min(n_runs, 2)))
        risk_create_from_typed_mean, risk_create_from_typed_std = _multi_sample(
            bench_risk_create_command_from_typed, max(1, min(n_runs, 2))
        )
        risk_gw_typed_ratio_mean, risk_gw_typed_ratio_std = _multi_sample(
            bench_risk_gateway_typed_ratio_heavy, max(1, min(n_runs, 2))
        )
    else:
        risk_typed_view_mean = risk_typed_view_std = None
        risk_create_from_typed_mean = risk_create_from_typed_std = None
        risk_gw_typed_ratio_mean = risk_gw_typed_ratio_std = None
    if include_feature_rust:
        feature_rust_mean, feature_rust_std = _multi_sample(bench_feature_engine_lob_update_rust, max(1, min(n_runs, 2)))
        feature_py_rust_parity_mean, feature_py_rust_parity_std = _multi_sample(
            bench_feature_engine_python_vs_rust_parity_mismatch_rate, max(1, min(n_runs, 2))
        )
    else:
        feature_rust_mean = feature_rust_std = None
        feature_py_rust_parity_mean = feature_py_rust_parity_std = None

    results = {
        "strategy_noop_metrics_on_us_per_event": noop_mean,
        "risk_evaluate_us_per_call": risk_eval_mean,
        "risk_evaluate_typed_frame_us_per_call": risk_eval_typed_mean,
        "risk_run_approve_us_per_intent": risk_run_approve_mean,
        "risk_run_reject_us_per_intent": risk_run_reject_mean,
        "gateway_process_envelope_obj_us_per_call": gw_obj_mean,
        "gateway_process_envelope_typed_us_per_call": gw_typed_mean,
        "research_backtest_us_per_row": bt_mean,
        "research_search_ms_per_trial": search_mean,
        "shioaji_callback_dispatch_us_per_event": cb_dispatch_mean,
        "market_data_callback_parse_us_per_event": cb_parse_mean,
        "shioaji_callback_soak_us_per_dispatch": cb_soak_us_mean,
        "shioaji_callback_soak_calls_per_dispatch": cb_soak_calls_mean,
        "feature_engine_lob_stats_us_per_event": feature_stats_mean,
        "feature_engine_lob_update_us_per_event": feature_l1_mean,
        "feature_engine_parity_mismatch_rate": feature_parity_mean,
        "recorder_batcher_add_us_per_row": rec_batcher_mean,
        "recorder_map_tick_us_per_event": rec_tick_mean,
        "recorder_map_bidask_us_per_event": rec_bidask_mean,
    }
    if include_risk_heavy:
        results["risk_typed_frame_view_us_per_call"] = risk_typed_view_mean
        results["risk_create_command_from_typed_us_per_call"] = risk_create_from_typed_mean
        results["risk_gateway_typed_vs_obj_ratio"] = risk_gw_typed_ratio_mean
    if include_recorder_io:
        results["recorder_ck_columnar_write_ms"] = rec_ck_mean
        results["recorder_wal_atomic_stress_ratio"] = rec_wal_stress_mean
    if include_feature_rust:
        results["feature_engine_lob_update_rust_us_per_event"] = feature_rust_mean
        results["feature_engine_python_vs_rust_parity_mismatch_rate"] = feature_py_rust_parity_mean
    stdevs = {
        "strategy_noop_metrics_on_us_per_event": noop_std,
        "risk_evaluate_us_per_call": risk_eval_std,
        "risk_evaluate_typed_frame_us_per_call": risk_eval_typed_std,
        "risk_run_approve_us_per_intent": risk_run_approve_std,
        "risk_run_reject_us_per_intent": risk_run_reject_std,
        "gateway_process_envelope_obj_us_per_call": gw_obj_std,
        "gateway_process_envelope_typed_us_per_call": gw_typed_std,
        "research_backtest_us_per_row": bt_std,
        "research_search_ms_per_trial": search_std,
        "shioaji_callback_dispatch_us_per_event": cb_dispatch_std,
        "market_data_callback_parse_us_per_event": cb_parse_std,
        "shioaji_callback_soak_us_per_dispatch": cb_soak_us_std,
        "shioaji_callback_soak_calls_per_dispatch": cb_soak_calls_std,
        "feature_engine_lob_stats_us_per_event": feature_stats_std,
        "feature_engine_lob_update_us_per_event": feature_l1_std,
        "feature_engine_parity_mismatch_rate": feature_parity_std,
        "recorder_batcher_add_us_per_row": rec_batcher_std,
        "recorder_map_tick_us_per_event": rec_tick_std,
        "recorder_map_bidask_us_per_event": rec_bidask_std,
    }
    if include_risk_heavy:
        stdevs["risk_typed_frame_view_us_per_call"] = risk_typed_view_std
        stdevs["risk_create_command_from_typed_us_per_call"] = risk_create_from_typed_std
        stdevs["risk_gateway_typed_vs_obj_ratio"] = risk_gw_typed_ratio_std
    if include_recorder_io:
        stdevs["recorder_ck_columnar_write_ms"] = rec_ck_std
        stdevs["recorder_wal_atomic_stress_ratio"] = rec_wal_stress_std
    if include_feature_rust:
        stdevs["feature_engine_lob_update_rust_us_per_event"] = feature_rust_std
        stdevs["feature_engine_python_vs_rust_parity_mismatch_rate"] = feature_py_rust_parity_std

    checks = [
        (
            "strategy_noop_metrics_on_us_per_event",
            "strategy_noop_metrics_on_us_per_event_max",
        ),
        (
            "risk_evaluate_us_per_call",
            "risk_evaluate_us_per_call_max",
        ),
        (
            "risk_evaluate_typed_frame_us_per_call",
            "risk_evaluate_typed_frame_us_per_call_max",
        ),
        (
            "risk_run_approve_us_per_intent",
            "risk_run_approve_us_per_intent_max",
        ),
        (
            "risk_run_reject_us_per_intent",
            "risk_run_reject_us_per_intent_max",
        ),
        (
            "gateway_process_envelope_obj_us_per_call",
            "gateway_process_envelope_obj_us_per_call_max",
        ),
        (
            "gateway_process_envelope_typed_us_per_call",
            "gateway_process_envelope_typed_us_per_call_max",
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
            "feature_engine_lob_stats_us_per_event",
            "feature_engine_lob_stats_us_per_event_max",
        ),
        (
            "feature_engine_lob_update_us_per_event",
            "feature_engine_lob_update_us_per_event_max",
        ),
        (
            "feature_engine_parity_mismatch_rate",
            "feature_engine_parity_mismatch_rate_max",
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
    if include_risk_heavy:
        checks.extend(
            [
                ("risk_typed_frame_view_us_per_call", "risk_typed_frame_view_us_per_call_max"),
                ("risk_create_command_from_typed_us_per_call", "risk_create_command_from_typed_us_per_call_max"),
                ("risk_gateway_typed_vs_obj_ratio", "risk_gateway_typed_vs_obj_ratio_max"),
            ]
        )
    if include_feature_rust:
        checks.extend(
            [
                ("feature_engine_lob_update_rust_us_per_event", "feature_engine_lob_update_rust_us_per_event_max"),
                (
                    "feature_engine_python_vs_rust_parity_mismatch_rate",
                    "feature_engine_python_vs_rust_parity_mismatch_rate_max",
                ),
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
