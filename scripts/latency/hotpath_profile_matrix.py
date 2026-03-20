#!/usr/bin/env python3
"""Hotpath Profiling Matrix — Unit 11.

Measures per-stage latency across the HFT platform hot path:
  normalizer → LOB engine → feature engine → strategy dispatch → risk evaluation

Uses synthetic events and time.perf_counter_ns() for nanosecond precision.
No real broker connection or market data feed required.

Usage:
    uv run python scripts/latency/hotpath_profile_matrix.py
    uv run python scripts/latency/hotpath_profile_matrix.py --iterations 50000
    uv run python scripts/latency/hotpath_profile_matrix.py --json
    uv run python scripts/latency/hotpath_profile_matrix.py --json --out results.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from time import perf_counter_ns
from typing import Any, Callable

import numpy as np

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("hotpath_profile_matrix")

# ---------------------------------------------------------------------------
# Optional platform imports — graceful degradation when platform not installed
# ---------------------------------------------------------------------------

try:
    from hft_platform.events import BidAskEvent, LOBStatsEvent, MetaData, TickEvent
    _EVENTS_AVAILABLE = True
except ImportError:
    _EVENTS_AVAILABLE = False
    log.warning("hft_platform.events not importable — will use stub event objects")

try:
    from hft_platform.feed_adapter.normalizer import MarketDataNormalizer
    _NORMALIZER_AVAILABLE = True
except ImportError:
    _NORMALIZER_AVAILABLE = False
    log.warning("MarketDataNormalizer not importable — normalizer stage will be skipped")

try:
    from hft_platform.feed_adapter.lob_engine import LOBEngine
    _LOB_ENGINE_AVAILABLE = True
except ImportError:
    _LOB_ENGINE_AVAILABLE = False
    log.warning("LOBEngine not importable — LOB stage will be skipped")

try:
    from hft_platform.feature.engine import FeatureEngine
    _FEATURE_ENGINE_AVAILABLE = True
except ImportError:
    _FEATURE_ENGINE_AVAILABLE = False
    log.warning("FeatureEngine not importable — feature stage will be skipped")

try:
    from hft_platform.risk.engine import RiskEngine
    _RISK_ENGINE_AVAILABLE = True
except ImportError:
    _RISK_ENGINE_AVAILABLE = False
    log.warning("RiskEngine not importable — risk stage will be skipped")

# ---------------------------------------------------------------------------
# Synthetic event factories
# ---------------------------------------------------------------------------

_SYMBOL = "2330"
_PRICE_SCALED = 5950000  # 595.0 * 10000
_TS_NS = 1_700_000_000_000_000_000  # fixed synthetic timestamp


def _make_meta(seq: int = 1) -> Any:
    if _EVENTS_AVAILABLE:
        return MetaData(seq=seq, source_ts=_TS_NS, local_ts=_TS_NS, topic=_SYMBOL)
    # Minimal stub
    class _Meta:
        def __init__(self) -> None:
            self.seq = seq
            self.source_ts = _TS_NS
            self.local_ts = _TS_NS
            self.topic = _SYMBOL
    return _Meta()


def _make_tick_event(seq: int = 1) -> Any:
    meta = _make_meta(seq)
    if _EVENTS_AVAILABLE:
        return TickEvent(
            meta=meta,
            symbol=_SYMBOL,
            price=_PRICE_SCALED,
            volume=100,
            total_volume=1000,
        )
    class _Tick:
        def __init__(self) -> None:
            self.meta = meta
            self.symbol = _SYMBOL
            self.price = _PRICE_SCALED
            self.volume = 100
            self.total_volume = 1000
    return _Tick()


def _make_bidask_event(seq: int = 1) -> Any:
    meta = _make_meta(seq)
    bids = np.array([[_PRICE_SCALED - 10000, 500], [_PRICE_SCALED - 20000, 300]], dtype=np.int64)
    asks = np.array([[_PRICE_SCALED + 10000, 400], [_PRICE_SCALED + 20000, 200]], dtype=np.int64)
    if _EVENTS_AVAILABLE:
        return BidAskEvent(
            meta=meta,
            symbol=_SYMBOL,
            bids=bids,
            asks=asks,
            is_snapshot=True,
        )
    class _BidAsk:
        def __init__(self) -> None:
            self.meta = meta
            self.symbol = _SYMBOL
            self.bids = bids
            self.asks = asks
            self.is_snapshot = True
            self.stats = None
            self.fused_stats = None
    return _BidAsk()


def _make_lob_stats_event() -> Any:
    if _EVENTS_AVAILABLE:
        return LOBStatsEvent(
            symbol=_SYMBOL,
            ts=_TS_NS,
            imbalance=0.15,
            best_bid=_PRICE_SCALED - 10000,
            best_ask=_PRICE_SCALED + 10000,
            bid_depth=500,
            ask_depth=400,
        )
    class _Stats:
        def __init__(self) -> None:
            self.symbol = _SYMBOL
            self.ts = _TS_NS
            self.imbalance = 0.15
            self.best_bid = _PRICE_SCALED - 10000
            self.best_ask = _PRICE_SCALED + 10000
            self.bid_depth = 500
            self.ask_depth = 400
            self.mid_price = _PRICE_SCALED / 10000.0
            self.spread = 2.0
            self.mid_price_x2 = _PRICE_SCALED * 2
            self.spread_scaled = 20000
    return _Stats()


# ---------------------------------------------------------------------------
# Stage result containers
# ---------------------------------------------------------------------------

@dataclass
class StageResult:
    stage: str
    available: bool
    iterations: int
    samples_ns: list[int] = field(default_factory=list)
    errors: int = 0
    skipped_reason: str = ""

    def p50_ns(self) -> float:
        return float(np.percentile(self.samples_ns, 50)) if self.samples_ns else 0.0

    def p95_ns(self) -> float:
        return float(np.percentile(self.samples_ns, 95)) if self.samples_ns else 0.0

    def p99_ns(self) -> float:
        return float(np.percentile(self.samples_ns, 99)) if self.samples_ns else 0.0

    def mean_ns(self) -> float:
        return mean(self.samples_ns) if self.samples_ns else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "available": self.available,
            "iterations": self.iterations,
            "samples_collected": len(self.samples_ns),
            "errors": self.errors,
            "skipped_reason": self.skipped_reason,
            "latency_ns": {
                "p50": round(self.p50_ns(), 1),
                "p95": round(self.p95_ns(), 1),
                "p99": round(self.p99_ns(), 1),
                "mean": round(self.mean_ns(), 1),
            } if self.samples_ns else None,
        }


# ---------------------------------------------------------------------------
# Profiling helpers
# ---------------------------------------------------------------------------

def _time_stage(
    fn: Callable[[], Any],
    n: int,
    warmup: int = 200,
) -> tuple[list[int], int]:
    """Run fn n times, return (latency_samples_ns, error_count).

    Performs warmup iterations before measurement to allow JIT / cache warm-up.
    """
    errors = 0
    # Warmup
    for _ in range(warmup):
        try:
            fn()
        except Exception:
            pass

    samples: list[int] = []
    for _ in range(n):
        t0 = perf_counter_ns()
        try:
            fn()
            t1 = perf_counter_ns()
            samples.append(t1 - t0)
        except Exception:
            errors += 1
    return samples, errors


# ---------------------------------------------------------------------------
# Individual stage profilers
# ---------------------------------------------------------------------------

def profile_normalizer(n: int) -> StageResult:
    result = StageResult(stage="normalizer", available=_NORMALIZER_AVAILABLE, iterations=n)

    if not _NORMALIZER_AVAILABLE:
        result.skipped_reason = "MarketDataNormalizer not importable"
        return result

    try:
        # Build a minimal normalizer — no broker SDK required
        normalizer = MarketDataNormalizer.__new__(MarketDataNormalizer)
        # Attempt minimal init — may vary; fall back to dict-lookup approach
        try:
            normalizer.__init__()  # type: ignore[misc]
        except Exception:
            pass

        # Build a realistic raw payload dict (Shioaji-style raw tick format)
        raw_tick: dict[str, Any] = {
            "Symbol": _SYMBOL,
            "Close": [595.0],
            "Volume": [100],
            "TotalVolume": [1000],
            "TickType": [1],
            "Time": "09:00:00.000000",
            "Date": "2026/03/19",
            "Simtrade": [0],
        }

        def _run() -> None:
            try:
                normalizer.normalize_tick(raw_tick)
            except Exception:
                pass

        samples, errors = _time_stage(_run, n)
        result.samples_ns = samples
        result.errors = errors
    except Exception as exc:
        result.skipped_reason = f"normalizer init failed: {exc}"

    return result


def profile_lob_engine(n: int) -> StageResult:
    result = StageResult(stage="lob_engine", available=_LOB_ENGINE_AVAILABLE, iterations=n)

    if not _LOB_ENGINE_AVAILABLE:
        result.skipped_reason = "LOBEngine not importable"
        return result

    try:
        engine = LOBEngine()
        event = _make_bidask_event()

        def _run() -> None:
            engine.process_event(event)

        samples, errors = _time_stage(_run, n)
        result.samples_ns = samples
        result.errors = errors
    except Exception as exc:
        result.skipped_reason = f"LOBEngine setup failed: {exc}"

    return result


def profile_feature_engine(n: int) -> StageResult:
    result = StageResult(stage="feature_engine", available=_FEATURE_ENGINE_AVAILABLE, iterations=n)

    if not _FEATURE_ENGINE_AVAILABLE:
        result.skipped_reason = "FeatureEngine not importable"
        return result

    try:
        engine = FeatureEngine(emit_events=False)
        stats = _make_lob_stats_event()

        def _run() -> None:
            engine.process_lob_stats(stats)

        samples, errors = _time_stage(_run, n)
        result.samples_ns = samples
        result.errors = errors
    except Exception as exc:
        result.skipped_reason = f"FeatureEngine setup failed: {exc}"

    return result


def profile_strategy_dispatch(n: int) -> StageResult:
    """Profile a minimal synchronous strategy dispatch path.

    StrategyRunner.process_event is async and requires a running event loop.
    We profile the synchronous inner loop kernel: strategy list iteration +
    handle_event call on a no-op strategy.
    """
    result = StageResult(stage="strategy_dispatch", available=True, iterations=n)

    try:
        from hft_platform.strategy.base import BaseStrategy, StrategyContext

        class _NoOpStrategy(BaseStrategy):
            @property
            def strategy_id(self) -> str:
                return "noop"

            @property
            def enabled(self) -> bool:
                return True

            @enabled.setter
            def enabled(self, v: bool) -> None:
                pass

            def handle_event(self, event: Any, ctx: StrategyContext) -> list:
                return []

        strat = _NoOpStrategy()
        ctx = StrategyContext(
            symbol=_SYMBOL,
            positions={},
            lob=None,
            feature_snapshot=None,
        )
        event = _make_tick_event()

        def _run() -> None:
            strat.handle_event(event, ctx)

        samples, errors = _time_stage(_run, n)
        result.samples_ns = samples
        result.errors = errors
    except Exception as exc:
        result.available = False
        result.skipped_reason = f"strategy_dispatch setup failed: {exc}"

    return result


def profile_risk_evaluate(n: int) -> StageResult:
    """Profile RiskEngine.evaluate on a minimal synthetic OrderIntent."""
    result = StageResult(stage="risk_evaluate", available=_RISK_ENGINE_AVAILABLE, iterations=n)

    if not _RISK_ENGINE_AVAILABLE:
        result.skipped_reason = "RiskEngine not importable"
        return result

    try:
        import asyncio
        import tempfile

        import yaml

        minimal_config: dict[str, Any] = {
            "risk": {
                "max_order_qty": 10000,
                "max_notional": 100_000_000,
                "price_band_pct": 0.05,
            }
        }

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(minimal_config, f)
            cfg_path = f.name

        intent_q: asyncio.Queue = asyncio.Queue()
        order_q: asyncio.Queue = asyncio.Queue()
        engine = RiskEngine(config_path=cfg_path, intent_queue=intent_q, order_queue=order_q)

        from hft_platform.contracts.strategy import IntentType, OrderIntent, Side

        intent = OrderIntent(
            strategy_id="noop",
            symbol=_SYMBOL,
            price=_PRICE_SCALED,
            qty=100,
            side=Side.BUY,
            intent_type=IntentType.NEW,
            idempotency_key="test-001",
            ttl_ns=int(1e9),
        )

        def _run() -> None:
            engine.evaluate(intent)

        samples, errors = _time_stage(_run, n)
        result.samples_ns = samples
        result.errors = errors

        import os
        try:
            os.unlink(cfg_path)
        except OSError:
            pass

    except Exception as exc:
        result.available = False
        result.skipped_reason = f"RiskEngine setup failed: {exc}"

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_COL_WIDTHS = (22, 10, 10, 10, 10, 8, 8)
_HEADER = ("Stage", "P50 (ns)", "P95 (ns)", "P99 (ns)", "Mean (ns)", "Iter", "Errors")


def _fmt_row(values: tuple[str, ...], widths: tuple[int, ...]) -> str:
    return "  ".join(str(v).ljust(w) for v, w in zip(values, widths))


def print_ascii_table(results: list[StageResult]) -> None:
    sep = "-" * (sum(_COL_WIDTHS) + 2 * (len(_COL_WIDTHS) - 1))
    print()
    print("  Hotpath Profiling Matrix — Latency per Stage")
    print(sep)
    print(_fmt_row(_HEADER, _COL_WIDTHS))
    print(sep)
    for r in results:
        if r.samples_ns:
            row = (
                r.stage,
                f"{r.p50_ns():.0f}",
                f"{r.p95_ns():.0f}",
                f"{r.p99_ns():.0f}",
                f"{r.mean_ns():.0f}",
                str(len(r.samples_ns)),
                str(r.errors),
            )
        elif r.skipped_reason:
            row = (r.stage, "—", "—", "—", "—", "0", f"SKIP: {r.skipped_reason[:30]}")
        else:
            row = (r.stage, "—", "—", "—", "—", "0", str(r.errors))
        print(_fmt_row(row, _COL_WIDTHS))
    print(sep)
    print()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="HFT Platform hotpath per-stage latency profiler",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--iterations",
        "-n",
        type=int,
        default=10_000,
        help="Number of timed iterations per stage",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=200,
        help="Number of warmup iterations before timing (not counted)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
        help="Output results as JSON (to stdout)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write JSON output to file (implies --json)",
    )
    parser.add_argument(
        "--stages",
        nargs="+",
        default=None,
        choices=["normalizer", "lob_engine", "feature_engine", "strategy_dispatch", "risk_evaluate"],
        help="Restrict profiling to specified stages (default: all)",
    )
    return parser


_STAGE_PROFILERS: dict[str, Callable[[int], StageResult]] = {
    "normalizer": profile_normalizer,
    "lob_engine": profile_lob_engine,
    "feature_engine": profile_feature_engine,
    "strategy_dispatch": profile_strategy_dispatch,
    "risk_evaluate": profile_risk_evaluate,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    selected_stages = args.stages or list(_STAGE_PROFILERS)
    n = args.iterations
    emit_json = args.emit_json or (args.out is not None)

    log.info(
        "Starting hotpath profile matrix",
        iterations=n,
        warmup=args.warmup,
        stages=selected_stages,
    )

    results: list[StageResult] = []
    for stage_name in selected_stages:
        profiler = _STAGE_PROFILERS[stage_name]
        log.info("Profiling stage: %s (%d iterations)", stage_name, n)
        t_wall_start = time.monotonic()
        result = profiler(n)
        t_wall_elapsed = time.monotonic() - t_wall_start
        log.info(
            "Stage done",
            stage=stage_name,
            collected=len(result.samples_ns),
            errors=result.errors,
            wall_s=f"{t_wall_elapsed:.2f}",
        )
        results.append(result)

    if emit_json:
        payload: dict[str, Any] = {
            "schema": "hotpath_profile_matrix_v1",
            "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "config": {
                "iterations": n,
                "warmup": args.warmup,
                "stages": selected_stages,
            },
            "stages": [r.to_dict() for r in results],
        }
        json_str = json.dumps(payload, indent=2)
        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json_str)
            log.info("JSON written to %s", args.out)
        else:
            print(json_str)
    else:
        print_ascii_table(results)

    # Exit non-zero only if all stages skipped (platform not installed)
    all_skipped = all(not r.samples_ns for r in results)
    if all_skipped:
        log.error("All stages skipped — platform imports unavailable")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
