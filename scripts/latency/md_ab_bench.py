"""P5 A/B capacity bench: the market-data path on CPython 3.12 vs 3.14t, single loop vs sharded workers.

Drives the REAL MarketDataService path at a fixed offered rate with a production-shaped symbol mix:

    producer thread (broker stand-in)
      -> MarketDataService._on_shioaji_event
      -> N=0: call_soon_threadsafe -> raw_queue -> run() -> _process_raw      (single loop)
      -> N>=1: ShardedMDPipeline.offer -> worker threads (normalize, LOB, feature) -> loop commit
      -> real MarketDataNormalizer, LOBEngine, FeatureEngine, committed view, RingBufferBus publish,
         real recorder mapper and Redis payload build

The raw payloads are Shioaji-shaped tick/bidask dicts. The recorder queue and the Redis publisher are counting
stubs (the mapping and payload build are real, only the sink is stubbed). The bus has no consumer and the SHM
snapshot writer is a stub: a real one would create the segment the running monitor reads.

Workload: ~2,900 symbols; options 74% / futures 13% / stocks 12% of events; futures concentrated so the ten
hottest symbols carry ~15% of events. The schedule is seeded: every arm at one rate sees the same data
(workload_digest in each result proves it).

Arms (see DEFAULT_ARMS): 3.12 single loop (N=0); 3.14t single loop (N=0); 3.14t N=1; 3.14t N in {2, 4, 8}.
Each arm x rate x repeat runs in its own subprocess pinned to the same cpuset. A run warms up (2 events per
symbol, unpaced, then waits for the pipeline to go quiet), then offers the schedule for --seconds.

Per run it reports: committed ev/s, drops, conservation (offered = committed + dropped + stale), engine CPU
(process CPU minus the bench producer thread, whose pacing sleeps are a bench artifact) per committed event, queue age p50/p99 (producer stamp -> start of normalization, so it includes the
call_soon_threadsafe hop in N=0 and the inbox wait in N>=1), loop probe lag p50/p99/max (a 1 ms
asyncio.sleep overshoot on the loop that commits), gil_enabled and the Python version.

A run is "sustained" only if ALL hold: zero drops; loop probe lag p99 < 5 ms; offered = committed + dropped +
stale; the pipeline kept up (committed by the end of the producer window >= 99% of offered, so a deep queue
that has not overflowed yet is not "sustained"); the producer achieved >= 98% of the nominal rate; the GIL
state matches the arm (3.14t arms must run with the GIL off).

Gate (pre-registered): PASS if some 3.14t arm is sustained in ALL repeats at a step >= 11,500 ev/s AND the
3.14t N=0 per-event CPU (engine CPU / committed events, median of repeats) is within +15% of 3.12 N=0, read at
the highest step where both N=0 arms are sustained in every repeat (lowest common step if none). A gate with missing data is INCOMPLETE, never PASS.

Usage:
    uv run python scripts/latency/md_ab_bench.py sweep --out-dir DIR [--seconds 30] [--repeats 3]
    uv run python scripts/latency/md_ab_bench.py summarize --in-dir DIR
    PYTHONPATH=src python scripts/latency/md_ab_bench.py run --workers 4 --rate 8000 --out one.json

Limits: synthetic payloads are dicts (the normalizer's dict path); no bus consumers, no strategy, no order
path; the heap is far smaller than production's (GC behavior is a separate gate); WSL2 CPUs may be shared.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import contextlib
import gc
import hashlib
import heapq
import importlib.util
import itertools
import json
import logging
import math
import os
import platform
import random
import statistics
import subprocess
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

SCHEMA = 1
TARGET_EV_S = 11_500
LAG_P99_LIMIT_US = 5_000.0
COST_TOLERANCE = 0.15
KEEP_UP_MIN_FRACTION = 0.99
RATE_ACHIEVED_MIN_FRACTION = 0.98
DEFAULT_RATES = (2_000, 4_000, 8_000, 11_500, 16_000)
DEFAULT_SECONDS = 30.0
DEFAULT_REPEATS = 3
DEFAULT_QUEUE_SIZE = 65_536  # bootstrap DEFAULT_RAW_QUEUE_SIZE for one quote connection
DEFAULT_CPUS = "0-7"
DEFAULT_SEED = 20_261_123
DEFAULT_SYMBOLS = 2_900
DEFAULT_BIDASK_FRACTION = 0.5
DEFAULT_WARMUP_PER_SYMBOL = 2
PROBE_INTERVAL_S = 0.001
DRAIN_POLL_S = 0.005
DRAIN_TIMEOUT_S = 30.0
MAX_PRODUCER_SLEEP_S = 0.002
IDLE_PROBE_S = 0.5
CHILD_TIMEOUT_MARGIN_S = 180.0
EXCHANGE = "TAIFEX"
PY312 = "py312"
PY314T = "py314t"
DEFAULT_PY314T = "~/hft_worktrees/_py314t_runtime_venv/bin/python"
ENV_ALLOWLIST = (
    "HFT_MD_WORKERS",
    "HFT_MD_COMMIT_BUDGET",
    "HFT_MD_YIELD_EVERY",
    "HFT_FEATURE_ENGINE_ENABLED",
    "HFT_MD_RECORD_DIRECT",
    "HFT_MONITOR_LIVE_ENABLED",
    "HFT_OBS_POLICY",
    "HFT_GC_DISABLE_TRADING",
)

GROUP_FUTURE = "future"
GROUP_STOCK = "stock"
GROUP_OPTION = "option"
FUTURE_ROOTS = ("TXF", "MXF", "TMF", "EXF", "FXF", "GTF")
FUTURE_EXPIRIES = ("L6", "A7", "B7", "C7", "F7", "I7", "L7", "A8")
OPTION_MONTHS = ((12, 6), (1, 7), (2, 7), (3, 7), (4, 7), (5, 7), (6, 7), (9, 7), (12, 7), (3, 8))
OPTION_STRIKES = tuple(range(16_000, 30_001, 100))
OPTION_ATM = 23_000
OPTION_TICK_BANDS = ((10.0, 0.1), (50.0, 0.5), (500.0, 1.0), (1_000.0, 5.0))
STOCK_TICK_BANDS = ((10.0, 0.01), (50.0, 0.05), (100.0, 0.1), (500.0, 0.5), (1_000.0, 1.0))
BOOK_LEVELS = 5


@dataclass(frozen=True)
class Arm:
    name: str
    interpreter: str
    workers: int


DEFAULT_ARMS = (
    Arm("py312-n0", PY312, 0),
    Arm("py314t-n0", PY314T, 0),
    Arm("py314t-n1", PY314T, 1),
    Arm("py314t-n2", PY314T, 2),
    Arm("py314t-n4", PY314T, 4),
    Arm("py314t-n8", PY314T, 8),
)


# -- argument parsing --------------------------------------------------------------------------------------


def parse_cpus(text: str) -> tuple[int, ...]:
    """Parse a cpuset like "0-7" or "0,2,4-5" into a sorted tuple. Raises ValueError on a bad element."""
    cpus: set[int] = set()
    for part in text.split(","):
        element = part.strip()
        if not element:
            raise ValueError("empty cpu list element")
        low_text, dash, high_text = element.partition("-")
        low = int(low_text)
        high = int(high_text) if dash else low
        if low < 0 or high < low:
            raise ValueError(f"bad cpu range {element!r}")
        cpus.update(range(low, high + 1))
    return tuple(sorted(cpus))


def parse_rates(text: str) -> tuple[int, ...]:
    rates = sorted({int(part) for part in text.split(",") if part.strip()})
    if not rates or rates[0] <= 0:
        raise ValueError("rates must be positive integers")
    return tuple(rates)


def select_arms(text: str, known: Sequence[Arm] = DEFAULT_ARMS) -> tuple[Arm, ...]:
    by_name = {arm.name: arm for arm in known}
    chosen = []
    for name in (part.strip() for part in text.split(",") if part.strip()):
        if name not in by_name:
            raise ValueError(f"unknown arm {name!r}; known: {sorted(by_name)}")
        chosen.append(by_name[name])
    if not chosen:
        raise ValueError("no arms selected")
    return tuple(chosen)


def _wrap(parser: Callable[[str], Any]) -> Callable[[str], Any]:
    def convert(text: str) -> Any:
        try:
            return parser(text)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc

    return convert


def _positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise ValueError("must be an integer > 0")
    return value


def _nonnegative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise ValueError("must be an integer >= 0")
    return value


def _positive_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("must be a finite number > 0")
    return value


def _fraction(text: str) -> float:
    value = float(text)
    if not 0.0 <= value <= 1.0:
        raise ValueError("must be within [0, 1]")
    return value


def _add_workload_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--symbols", type=_wrap(_positive_int), default=DEFAULT_SYMBOLS, help="symbol universe size")
    parser.add_argument("--bidask-fraction", type=_wrap(_fraction), default=DEFAULT_BIDASK_FRACTION)
    parser.add_argument("--seed", type=_wrap(_nonnegative_int), default=DEFAULT_SEED)
    parser.add_argument("--queue-size", type=_wrap(_positive_int), default=DEFAULT_QUEUE_SIZE, help="raw_queue maxsize")
    parser.add_argument("--warmup-per-symbol", type=_wrap(_nonnegative_int), default=DEFAULT_WARMUP_PER_SYMBOL)
    parser.add_argument("--drain-timeout", type=_wrap(_positive_float), default=DRAIN_TIMEOUT_S)
    parser.add_argument("--cpus", type=_wrap(parse_cpus), default=parse_cpus(DEFAULT_CPUS), help="cpuset, e.g. 0-7")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="P5 A/B market-data capacity bench (3.12 vs 3.14t).")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="one measured run in this interpreter (the sweep spawns these)")
    _add_workload_args(run)
    run.add_argument("--workers", type=_wrap(_nonnegative_int), required=True, help="HFT_MD_WORKERS for this run")
    run.add_argument("--rate", type=_wrap(_positive_int), required=True, help="offered events/s")
    run.add_argument("--seconds", type=_wrap(_positive_float), default=DEFAULT_SECONDS)
    run.add_argument("--arm", default="adhoc", help="label stored in the result")
    run.add_argument("--interpreter", default="adhoc", help="label stored in the result (py312 / py314t)")
    run.add_argument("--repeat", type=_wrap(_positive_int), default=1)
    run.add_argument("--expect-gil", choices=("on", "off", "any"), default="any")
    run.add_argument("--out", type=Path, required=True)

    sweep = sub.add_parser("sweep", help="every arm x rate x repeat, then the summary and the gate verdict")
    _add_workload_args(sweep)
    sweep.add_argument("--out-dir", type=Path, required=True)
    sweep.add_argument("--rates", type=_wrap(parse_rates), default=DEFAULT_RATES)
    sweep.add_argument("--seconds", type=_wrap(_positive_float), default=DEFAULT_SECONDS)
    sweep.add_argument("--repeats", type=_wrap(_positive_int), default=DEFAULT_REPEATS)
    sweep.add_argument("--arms", type=_wrap(select_arms), default=DEFAULT_ARMS, help="comma list of arm names")
    sweep.add_argument("--py312", default=sys.executable, help="3.12 interpreter (default: this one)")
    sweep.add_argument("--py314t", default=os.path.expanduser(DEFAULT_PY314T), help="3.14t interpreter")
    sweep.add_argument("--resume", action="store_true", help="reuse result files already in --out-dir")

    summ = sub.add_parser("summarize", help="rebuild the table and verdict from stored run results")
    summ.add_argument("--in-dir", type=Path, required=True)
    summ.add_argument("--repeats", type=_wrap(_positive_int), default=DEFAULT_REPEATS)
    summ.add_argument("--out", type=Path, help="also write the summary JSON here")
    return parser


# -- workload ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkloadSpec:
    n_symbols: int = DEFAULT_SYMBOLS
    n_futures: int = 30
    n_stocks: int = 60
    share_options: float = 0.74
    share_futures: float = 0.13
    share_stocks: float = 0.12
    top10_target: float = 0.15
    futures_zipf: float = 1.0
    stocks_zipf: float = 0.8
    bidask_fraction: float = DEFAULT_BIDASK_FRACTION
    variants: int = 4
    seed: int = DEFAULT_SEED

    @property
    def n_options(self) -> int:
        return self.n_symbols - self.n_futures - self.n_stocks


def zipf_weights(n: int, exponent: float) -> list[float]:
    raw = [1.0 / (rank**exponent) for rank in range(1, n + 1)]
    total = sum(raw)
    return [w / total for w in raw]


def combined_weights(spec: WorkloadSpec, options_exponent: float) -> list[float]:
    """Event weights in symbol order (futures, stocks, options); group shares are normalised to sum to 1."""
    total = spec.share_options + spec.share_futures + spec.share_stocks
    parts = (
        (spec.share_futures / total, zipf_weights(spec.n_futures, spec.futures_zipf)),
        (spec.share_stocks / total, zipf_weights(spec.n_stocks, spec.stocks_zipf)),
        (spec.share_options / total, zipf_weights(spec.n_options, options_exponent)),
    )
    return [share * w for share, weights in parts for w in weights]


def top_share(weights: Sequence[float], k: int = 10) -> float:
    return sum(heapq.nlargest(k, weights))


def solve_options_exponent(spec: WorkloadSpec) -> float:
    """Zipf exponent of the options group so the ten hottest symbols carry spec.top10_target of events."""
    low, high = 0.0, 4.0
    if top_share(combined_weights(spec, low)) >= spec.top10_target:
        return low
    if top_share(combined_weights(spec, high)) <= spec.top10_target:
        return high
    for _ in range(50):
        mid = (low + high) / 2.0
        if top_share(combined_weights(spec, mid)) < spec.top10_target:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def _option_symbols(count: int) -> list[str]:
    """Option codes ordered hottest first: near expiry and near the money."""
    ranked = []
    for exp_idx, (month, year) in enumerate(OPTION_MONTHS):
        call_letter = chr(ord("A") + month - 1)
        put_letter = chr(ord("M") + month - 1)
        for strike in OPTION_STRIKES:
            heat = abs(strike - OPTION_ATM) / 100.0 + 8.0 * exp_idx
            ranked.append((heat, f"TXO{strike}{call_letter}{year % 10}"))
            ranked.append((heat + 0.5, f"TXO{strike}{put_letter}{year % 10}"))
    ranked.sort()
    if count > len(ranked):
        raise ValueError(f"at most {len(ranked)} option symbols are generated, asked for {count}")
    return [symbol for _, symbol in ranked[:count]]


def build_symbols(spec: WorkloadSpec) -> tuple[list[str], list[str]]:
    """(symbols, groups) in weight order: futures, stocks, options."""
    if spec.n_options < 1:
        raise ValueError("n_symbols must exceed n_futures + n_stocks")
    futures = [f"{root}{exp}" for exp in FUTURE_EXPIRIES for root in FUTURE_ROOTS][: spec.n_futures]
    if len(futures) < spec.n_futures:
        raise ValueError("too many futures requested")
    stocks = [f"{1101 + 7 * i:04d}" for i in range(spec.n_stocks)]
    options = _option_symbols(spec.n_options)
    symbols = futures + stocks + options
    groups = [GROUP_FUTURE] * len(futures) + [GROUP_STOCK] * len(stocks) + [GROUP_OPTION] * len(options)
    return symbols, groups


def _tick_size(group: str, price: float) -> float:
    if group == GROUP_FUTURE:
        return 1.0
    bands, top = (OPTION_TICK_BANDS, 10.0) if group == GROUP_OPTION else (STOCK_TICK_BANDS, 5.0)
    for bound, tick in bands:
        if price < bound:
            return tick
    return top


def _snap(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 4)


def _base_price(group: str, rng: random.Random) -> float:
    if group == GROUP_FUTURE:
        return float(rng.randint(21_000, 23_000))
    low, high = (2.0, 900.0) if group == GROUP_OPTION else (10.0, 1_500.0)
    return math.exp(rng.uniform(math.log(low), math.log(high)))


def _symbol_variants(symbol: str, group: str, rng: random.Random, count: int) -> tuple[list[dict], list[dict]]:
    """Shioaji-shaped tick and bidask payloads (a short random walk of the best quote)."""
    base = _base_price(group, rng)
    tick = _tick_size(group, base)
    mid = _snap(base, tick)
    ticks: list[dict] = []
    bidasks: list[dict] = []
    for _ in range(count):
        mid = max(tick * (BOOK_LEVELS + 1), _snap(mid + tick * rng.randint(-3, 3), tick))
        ticks.append(
            {
                "code": symbol,
                "close": mid,
                "volume": rng.randint(1, 20),
                "total_volume": rng.randint(100, 50_000),
                "simtrade": 0,
                "intraday_odd": 0,
            }
        )
        bidasks.append(
            {
                "code": symbol,
                "bid_price": [round(mid - k * tick, 4) for k in range(BOOK_LEVELS)],
                "bid_volume": [rng.randint(1, 200) for _ in range(BOOK_LEVELS)],
                "ask_price": [round(mid + (k + 1) * tick, 4) for k in range(BOOK_LEVELS)],
                "ask_volume": [rng.randint(1, 200) for _ in range(BOOK_LEVELS)],
                "simtrade": 0,
            }
        )
    return ticks, bidasks


@dataclass
class Plan:
    events: list[dict]
    warmup: list[dict]
    symbols: list[str]
    groups: list[str]
    stats: dict[str, Any]
    digest: str


def _group_stats(spec: WorkloadSpec, groups: list[str], slots: list[int], kinds: list[bool]) -> dict[str, Any]:
    total = len(slots)
    realized: dict[str, int] = defaultdict(int)
    per_slot: dict[int, int] = defaultdict(int)
    for slot in slots:
        realized[groups[slot]] += 1
        per_slot[slot] += 1
    top10 = sum(heapq.nlargest(10, per_slot.values())) if per_slot else 0
    count = {g: groups.count(g) for g in (GROUP_FUTURE, GROUP_STOCK, GROUP_OPTION)}
    return {
        "events": total,
        "symbols": len(groups),
        "symbols_by_group": count,
        "share_realized": {g: round(realized[g] / total, 4) if total else None for g in count},
        "top10_share_realized": round(top10 / total, 4) if total else None,
        "bidask_share_realized": round(sum(kinds) / total, 4) if total else None,
        "symbols_seen": len(per_slot),
        "bidask_fraction": spec.bidask_fraction,
        "seed": spec.seed,
    }


def build_plan(spec: WorkloadSpec, rate: int, seconds: float, warmup_per_symbol: int) -> Plan:
    """Seeded schedule of payload references; identical for every arm at one rate and seconds."""
    symbols, groups = build_symbols(spec)
    exponent = solve_options_exponent(spec)
    weights = combined_weights(spec, exponent)
    pool_rng = random.Random(spec.seed)
    pool = [_symbol_variants(sym, grp, pool_rng, spec.variants) for sym, grp in zip(symbols, groups, strict=True)]
    total = int(rate * seconds)
    rng = random.Random(spec.seed + 1)
    slots = rng.choices(range(len(symbols)), cum_weights=list(itertools.accumulate(weights)), k=total)
    kinds = [rng.random() < spec.bidask_fraction for _ in range(total)]
    counters = [0] * (2 * len(symbols))
    events: list[dict] = []
    for slot, kind in zip(slots, kinds, strict=True):
        key = slot * 2 + kind
        seen = counters[key]
        counters[key] = seen + 1
        events.append(pool[slot][kind][seen % spec.variants])
    # Per symbol: bidask first (it seeds the book the tick classifier reads), then alternating.
    warmup = [
        pool[slot][(rep + 1) % 2][rep % spec.variants]
        for slot in range(len(symbols))
        for rep in range(warmup_per_symbol)
    ]
    random.Random(spec.seed + 2).shuffle(warmup)
    stats = _group_stats(spec, groups, slots, kinds)
    stats["options_zipf_exponent"] = round(exponent, 4)
    stats["top10_share_target"] = spec.top10_target
    stats["top10_share_weights"] = round(top_share(weights), 4)
    digest = hashlib.sha256(array.array("I", slots).tobytes() + bytes(kinds))
    digest.update(json.dumps(pool).encode("utf-8"))
    return Plan(events, warmup, symbols, groups, stats, digest.hexdigest()[:16])


# -- statistics, criteria, gate ----------------------------------------------------------------------------


def latency_stats_us(values_ns: Sequence[int]) -> dict[str, Any]:
    if not values_ns:
        return {"n": 0, "p50": None, "p99": None, "max": None}
    ordered = sorted(values_ns)
    last = len(ordered) - 1

    def pick(q: float) -> float:
        return round(ordered[round(q * last)] / 1000.0, 1)

    return {"n": len(ordered), "p50": pick(0.50), "p99": pick(0.99), "max": round(ordered[-1] / 1000.0, 1)}


def conservation(offered: int, committed: int, dropped: int, stale: int, rejected: int) -> dict[str, int]:
    """unaccounted = offered - (committed + dropped + stale); rejected and residual split it for diagnosis."""
    unaccounted = offered - (committed + dropped + stale)
    return {"unaccounted": unaccounted, "rejected": rejected, "residual": unaccounted - rejected}


def run_criteria(
    *,
    offered: int,
    planned: int,
    rate: int,
    producer_elapsed_s: float,
    producer_error: str | None,
    counts: dict[str, int],
    drained: bool,
    committed_at_end: int,
    lag_p99_us: float | None,
    gil_enabled: bool,
    expect_gil: str,
) -> dict[str, bool]:
    unaccounted = offered - (counts["committed"] + counts["dropped"] + counts["stale"])
    zero_drops = counts["dropped"] == 0
    lag_ok = lag_p99_us is not None and lag_p99_us < LAG_P99_LIMIT_US
    conserved = unaccounted == 0 and drained
    keeps_up = offered > 0 and committed_at_end >= KEEP_UP_MIN_FRACTION * offered
    achieved = offered / producer_elapsed_s if producer_elapsed_s > 0 else 0.0
    offered_ok = producer_error is None and offered == planned and achieved >= RATE_ACHIEVED_MIN_FRACTION * rate
    gil_ok = expect_gil == "any" or gil_enabled == (expect_gil == "on")
    literal = zero_drops and lag_ok and conserved
    return {
        "zero_drops": zero_drops,
        "lag_p99_ok": lag_ok,
        "conserved": conserved,
        "keeps_up": keeps_up,
        "offered_ok": offered_ok,
        "gil_ok": gil_ok,
        "literal_sustained": literal,
        "sustained": literal and keeps_up and offered_ok and gil_ok,
    }


def _median(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(statistics.median(present), 1) if present else None


def _worst(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return max(present) if present else None


def _sustained(record: dict[str, Any]) -> bool:
    return bool(record.get("criteria", {}).get("sustained", False))


def summarize(records: list[dict[str, Any]], repeats: int) -> dict[str, Any]:
    """Per arm x rate rows (median of repeats; p99 and max columns are the worst run) and per-arm capacity."""
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[(record["arm"], record["rate"])].append(record)
    rows = []
    for (arm, rate), runs in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        done = [r for r in runs if "error" not in r]
        rows.append(
            {
                "arm": arm,
                "interpreter": runs[0]["interpreter"],
                "workers": runs[0]["workers"],
                "rate": rate,
                "runs": len(runs),
                "completed": len(done),
                "sustained_runs": sum(1 for r in runs if _sustained(r)),
                "dropped_max": _worst([r["counts"]["dropped"] for r in done]),
                "committed_ev_s": _median([r["committed_ev_s"] for r in done]),
                "cpu_us_per_event": _median([r["cpu"]["us_per_event"] for r in done]),
                "queue_age_p50_us": _median([r["queue_age_us"]["p50"] for r in done]),
                "queue_age_p99_us": _worst([r["queue_age_us"]["p99"] for r in done]),
                "lag_p50_us": _median([r["loop_probe_lag_us"]["p50"] for r in done]),
                "lag_p99_us": _worst([r["loop_probe_lag_us"]["p99"] for r in done]),
                "lag_max_us": _worst([r["loop_probe_lag_us"]["max"] for r in done]),
                "conserved_all": bool(done) and all(r["criteria"]["conserved"] for r in done),
                "gil_enabled": done[0]["gil_enabled"] if done else None,
                "python": done[0]["python"] if done else None,
            }
        )
    arms: dict[str, dict[str, Any]] = {}
    for row in rows:
        arm = arms.setdefault(
            row["arm"], {"interpreter": row["interpreter"], "workers": row["workers"], "max_sustained_rate": None}
        )
        if row["runs"] >= repeats and row["sustained_runs"] == row["runs"]:
            arm["max_sustained_rate"] = max(arm["max_sustained_rate"] or 0, row["rate"])
    digests: dict[int, set[str]] = defaultdict(set)
    for record in records:
        if "workload_digest" in record:
            digests[record["rate"]].add(record["workload_digest"])
    return {
        "rows": rows,
        "arms": arms,
        "workload_identical_per_rate": {str(rate): len(found) == 1 for rate, found in sorted(digests.items())},
    }


def _arm_runs(records: list[dict[str, Any]], interpreter: str, workers: int | None = None) -> dict[int, list[dict]]:
    by_rate: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if record["interpreter"] == interpreter and (workers is None or record["workers"] == workers):
            by_rate[record["rate"]].append(record)
    return by_rate


def _capacity_gate(records: list[dict[str, Any]], target: int, repeats: int) -> dict[str, Any]:
    qualifying: dict[str, list[int]] = {}
    evaluable = False
    arm_names = sorted({r["arm"] for r in records if r["interpreter"] == PY314T})
    for arm in arm_names:
        by_rate: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for record in records:
            if record["arm"] == arm and record["rate"] >= target:
                by_rate[record["rate"]].append(record)
        for rate, runs in sorted(by_rate.items()):
            if len(runs) >= repeats:
                evaluable = True
                if all(_sustained(r) for r in runs):
                    qualifying.setdefault(arm, []).append(rate)
    passed: bool | None = bool(qualifying) if evaluable else None
    return {"passed": passed, "qualifying": qualifying, "target_ev_s": target}


def _cost_gate(records: list[dict[str, Any]], tolerance: float, repeats: int) -> dict[str, Any]:
    """3.14t N=0 vs 3.12 N=0 engine CPU per event.

    Reference step: the highest common step where both arms were sustained in every repeat (a busy loop that is not
    saturated, so idle epoll wakeups do not dominate the per-event figure); with none, the lowest common step.
    """
    base = _arm_runs(records, PY312, 0)
    new = _arm_runs(records, PY314T, 0)
    ratios: dict[str, float] = {}
    clean: list[int] = []
    for rate in sorted(set(base) & set(new)):
        base_cpu = _median([r["cpu"]["us_per_event"] for r in base[rate] if "error" not in r])
        new_cpu = _median([r["cpu"]["us_per_event"] for r in new[rate] if "error" not in r])
        if not base_cpu or not new_cpu:
            continue
        ratios[str(rate)] = round(new_cpu / base_cpu, 4)
        both = base[rate] + new[rate]
        if min(len(base[rate]), len(new[rate])) >= repeats and all(_sustained(r) for r in both):
            clean.append(rate)
    if not ratios:
        return {
            "passed": None,
            "reference_rate": None,
            "ratio": None,
            "limit": 1.0 + tolerance,
            "per_rate": {},
            "clean_steps": [],
        }
    reference = str(max(clean)) if clean else min(ratios, key=int)
    return {
        "passed": ratios[reference] <= 1.0 + tolerance,
        "reference_rate": int(reference),
        "ratio": ratios[reference],
        "limit": round(1.0 + tolerance, 4),
        "per_rate": ratios,
        "clean_steps": clean,
    }


def evaluate_gate(
    records: list[dict[str, Any]],
    *,
    target_rate: int = TARGET_EV_S,
    tolerance: float = COST_TOLERANCE,
    repeats: int = DEFAULT_REPEATS,
) -> dict[str, Any]:
    """PASS / FAIL / INCOMPLETE. INCOMPLETE means a half of the gate had no complete data to evaluate."""
    capacity = _capacity_gate(records, target_rate, repeats)
    cost = _cost_gate(records, tolerance, repeats)
    reasons = []
    if capacity["passed"] is None:
        reasons.append(f"no 3.14t arm has {repeats} complete repeats at a step >= {target_rate}")
    elif not capacity["passed"]:
        reasons.append(f"no 3.14t arm was sustained in all {repeats} repeats at a step >= {target_rate}")
    if cost["passed"] is None:
        reasons.append("N=0 per-event cost not comparable (3.12 or 3.14t N=0 runs missing)")
    elif not cost["passed"]:
        reasons.append(f"3.14t N=0 per-event CPU is {cost['ratio']}x of 3.12 (limit {cost['limit']}x)")
    if capacity["passed"] is False or cost["passed"] is False:
        status = "FAIL"
    elif capacity["passed"] is None or cost["passed"] is None:
        status = "INCOMPLETE"
    else:
        status = "PASS"
    return {"status": status, "capacity": capacity, "cost": cost, "reasons": reasons}


def _fmt(value: Any, width: int, digits: int = 1) -> str:
    if value is None:
        return "-".rjust(width)
    if isinstance(value, float):
        return f"{value:.{digits}f}".rjust(width)
    return str(value).rjust(width)


def format_report(summary: dict[str, Any], gate: dict[str, Any]) -> str:
    header = (
        f"{'arm':<11}{'rate':>6}{'ok/runs':>8}{'drops':>7}{'commit/s':>10}{'cpu_us/ev':>10}"
        f"{'qage50':>9}{'qage99w':>10}{'lag50':>8}{'lag99w':>9}{'lagmaxw':>9}{'gil':>7}  py"
    )
    lines = [
        "median of repeats; 'w' columns = worst run. qage = producer stamp -> start of normalization (us).",
        "lag = loop probe overshoot of a 1 ms sleep (us). cpu_us/ev = engine CPU per committed event.",
        header,
    ]
    for row in summary["rows"]:
        gil = {True: "on", False: "off", None: "-"}[row["gil_enabled"]]
        lines.append(
            f"{row['arm']:<11}{row['rate']:>6}{str(row['sustained_runs']) + '/' + str(row['runs']):>8}"
            f"{_fmt(row['dropped_max'], 7, 0)}{_fmt(row['committed_ev_s'], 10, 0)}{_fmt(row['cpu_us_per_event'], 10)}"
            f"{_fmt(row['queue_age_p50_us'], 9, 0)}{_fmt(row['queue_age_p99_us'], 10, 0)}"
            f"{_fmt(row['lag_p50_us'], 8, 0)}{_fmt(row['lag_p99_us'], 9, 0)}{_fmt(row['lag_max_us'], 9, 0)}"
            f"{gil:>7}  {row['python'] or '-'}"
        )
    lines.append("")
    lines.append("max sustained rate (drops==0, lag p99<5ms, conserved, kept up; all repeats):")
    for name, arm in summary["arms"].items():
        lines.append(f"  {name:<11} {arm['max_sustained_rate']}")
    cost = gate["cost"]
    lines.append(
        f"N=0 cost 3.14t/3.12 = {cost['ratio']} at {cost['reference_rate']} ev/s (limit {cost['limit']}); "
        f"per step: {cost['per_rate']}"
    )
    lines.append(f"capacity qualifying arms (>= {gate['capacity']['target_ev_s']}): {gate['capacity']['qualifying']}")
    lines.append(f"workload identical across arms per rate: {summary['workload_identical_per_rate']}")
    lines.append(f"VERDICT: {gate['status']}" + ("" if gate["status"] == "PASS" else "  (HOLD)"))
    lines.extend(f"  - {reason}" for reason in gate["reasons"])
    return "\n".join(lines)


# -- one measured run (child process) ----------------------------------------------------------------------


class _StubShm:
    """Accepts any constructor arguments and any call: a real writer would create the monitor's segment."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        return None

    def __getattr__(self, name: str) -> Callable[..., None]:
        return _noop


def _noop(*args: Any, **kwargs: Any) -> None:
    return None


class _CountingSink:
    """Recorder queue stand-in: the service maps the event for real, the put is only counted."""

    maxsize = 0

    def __init__(self) -> None:
        self.puts = 0

    def put_nowait(self, item: Any) -> None:
        self.puts += 1

    def qsize(self) -> int:
        return 0


class _CountingRedis:
    """Monitor Redis publisher stand-in: the payload is built for real, the publish is only counted."""

    def __init__(self) -> None:
        self.publishes = 0

    def publish_market_data(self, payload: Any) -> None:
        self.publishes += 1


@dataclass
class Instruments:
    """Counters and sample lists; each list has one writer thread (loop, or one worker)."""

    published: int = 0
    processed: int = 0
    age_ns: list[list[int]] = field(default_factory=list)
    lag_ns: list[int] = field(default_factory=list)


@dataclass
class ProducerStats:
    offered: int = 0
    planned: int = 0
    elapsed_ns: int = 0
    late_max_ns: int = 0
    late_sum_ns: int = 0
    late_over_1ms: int = 0
    cpu_ns: int = 0
    committed_at_end: int = 0
    error: str | None = None


def _produce(
    on_event: Callable[[str, dict], None],
    wall_ns: Callable[[], int],
    events: list[dict],
    rate: int,
    stats: ProducerStats,
) -> None:
    """Offer events at a fixed rate (rate 0: unpaced). Lateness = send time - scheduled time."""
    perf_ns = time.perf_counter_ns
    total = len(events)
    stats.planned = total
    start = perf_ns()
    index = 0
    try:
        while index < total:
            now = perf_ns()
            due = total if rate <= 0 else min(total, (now - start) * rate // 1_000_000_000 + 1)
            if index >= due:
                wait_ns = start + index * 1_000_000_000 // rate - now
                time.sleep(min(max(wait_ns / 1e9, 0.0), MAX_PRODUCER_SLEEP_S))
                continue
            while index < due:
                payload = dict(events[index])
                sent = perf_ns()
                payload["ts"] = wall_ns()
                payload["_t"] = sent
                on_event(EXCHANGE, payload)
                if rate > 0:
                    late = sent - (start + index * 1_000_000_000 // rate)
                    stats.late_sum_ns += late
                    stats.late_max_ns = max(stats.late_max_ns, late)
                    stats.late_over_1ms += late > 1_000_000
                index += 1
                stats.offered = index
    except BaseException as exc:
        # Recorded in stats.error and fails the offered_ok criterion; the run reports it, nothing is swallowed.
        stats.error = f"{type(exc).__name__}: {exc}"
    finally:
        stats.elapsed_ns = perf_ns() - start
        stats.cpu_ns = time.thread_time_ns()


def _build_service(workers: int, queue_size: int, plan: Plan) -> tuple[Any, _CountingSink, _CountingRedis]:
    os.environ["HFT_MD_WORKERS"] = str(workers)
    # Imported here, after the cpuset is pinned and HFT_MD_WORKERS is set: the service reads it in __init__.
    from hft_platform.engine.event_bus import RingBufferBus
    from hft_platform.services._md_sharded import ShardedMDPipeline
    from hft_platform.services.market_data import MarketDataService

    client = MagicMock()
    client.subscribed_symbols = list(plan.symbols)
    sink = _CountingSink()
    with patch("hft_platform.services.market_data.ShmSnapshotWriter", _StubShm):
        svc = MarketDataService(
            bus=RingBufferBus(),
            raw_queue=asyncio.Queue(maxsize=queue_size),
            client=client,
            recorder_queue=sink,
            feature_engine=None,
        )
    # Declare the synthetic universe the way symbols.yaml does (code, exchange, product_type): an undeclared
    # symbol takes the LOB unknown-symbol ingress path, which production does not.
    svc.symbol_metadata.apply_runtime_overlay(
        [
            {"code": sym, "exchange": "TSE" if grp == GROUP_STOCK else EXCHANGE, "product_type": grp}
            for sym, grp in zip(plan.symbols, plan.groups, strict=True)
        ]
    )
    redis = _CountingRedis()
    svc._redis_publisher = redis

    if not svc._record_direct or svc.feature_engine is None:
        raise RuntimeError("bench needs the recorder direct write and the feature engine on")
    pipe = svc._md_shards
    if workers == 0 and pipe is not None:
        raise RuntimeError("HFT_MD_WORKERS=0 but the service built a sharded pipeline")
    if workers > 0 and not (isinstance(pipe, ShardedMDPipeline) and pipe.n_workers == workers):
        raise RuntimeError(f"HFT_MD_WORKERS={workers} but the service has no matching sharded pipeline")
    return svc, sink, redis


def _instrument(svc: Any, ins: Instruments, workers: int) -> None:
    """Shadow three methods on the instance; the service looks them up at call time."""
    ins.age_ns = [[] for _ in range(max(1, workers + 1))]
    perf_ns = time.perf_counter_ns
    publish_events = svc._publish_events

    def counted_publish(event: Any, stats: Any, feature_update: Any) -> None:
        publish_events(event, stats, feature_update)
        ins.published += 1

    svc._publish_events = counted_publish
    if workers == 0:
        process_raw = svc._process_raw
        ages = ins.age_ns[0]

        def timed_process(raw: Any) -> None:
            ages.append(perf_ns() - raw["_t"])
            process_raw(raw)
            ins.processed += 1

        svc._process_raw = timed_process
        return
    pipe = svc._md_shards
    compute = pipe._compute

    def timed_compute(raw: Any, worker: Any) -> Any:
        ins.age_ns[worker.idx + 1].append(perf_ns() - raw["_t"])
        return compute(raw, worker)

    pipe._compute = timed_compute


def _counts(svc: Any, ins: Instruments, workers: int) -> dict[str, int]:
    if workers == 0:
        return {
            "committed": ins.published,
            "dropped": svc._raw_dropped_count,
            "stale": 0,
            "rejected": ins.processed - ins.published,
        }
    pipe = svc._md_shards
    shards = pipe._workers
    return {
        "committed": ins.published,
        "dropped": pipe.drop_totals()[0],
        "stale": pipe.stale_drained + sum(w.stale_skipped for w in shards),
        "rejected": sum(w.rejected for w in shards) + pipe.commit_errors,
    }


async def _wait_quiescent(svc: Any, ins: Instruments, workers: int, offered_total: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if sum(_counts(svc, ins, workers).values()) == offered_total:
            return True
        await asyncio.sleep(DRAIN_POLL_S)
    return False


async def _probe_once(sink: list[int]) -> None:
    """One loop probe sample: how far a 1 ms asyncio.sleep overshoots (ns)."""
    t0 = time.perf_counter_ns()
    await asyncio.sleep(PROBE_INTERVAL_S)
    sink.append(max(0, time.perf_counter_ns() - t0 - int(PROBE_INTERVAL_S * 1e9)))


async def _probe_idle(sink: list[int], seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        await _probe_once(sink)


async def _run_phase(
    svc: Any, ins: Instruments, wall_ns: Callable[[], int], events: list[dict], rate: int, lag_sink: list[int]
) -> ProducerStats:
    stats = ProducerStats()
    thread = threading.Thread(
        target=_produce,
        args=(svc._on_shioaji_event, wall_ns, events, rate, stats),
        name="md-ab-producer",
        daemon=True,
    )
    thread.start()
    while thread.is_alive():
        await _probe_once(lag_sink)
    stats.committed_at_end = ins.published
    thread.join()
    return stats


@dataclass
class Window:
    base: dict[str, int]
    final: dict[str, int]
    sink_puts: int
    redis_publishes: int
    drained: bool
    drain_s: float
    wall_s: float
    process_cpu_ns: int
    loop_cpu_ns: int
    warmup_offered: int
    warmup_dropped: int
    committed_at_end_delta: int
    idle_lag_ns: list[int]


async def _drive(
    svc: Any, ins: Instruments, sink: _CountingSink, redis: _CountingRedis, plan: Plan, args: argparse.Namespace
) -> tuple[ProducerStats, Window]:
    from hft_platform.core import timebase

    workers = args.workers
    with patch.object(svc, "_connect_sequence", AsyncMock()):
        run_task = asyncio.create_task(svc.run())
        await asyncio.sleep(0)
        warm = await _run_phase(svc, ins, timebase.now_ns, plan.warmup, 0, [])
        if not await _wait_quiescent(svc, ins, workers, warm.offered, args.drain_timeout):
            raise RuntimeError("warm-up did not drain")
        idle_lag: list[int] = []
        await _probe_idle(idle_lag, IDLE_PROBE_S)
        base = _counts(svc, ins, workers)
        sink0, redis0 = sink.puts, redis.publishes
        for samples in ins.age_ns:
            samples.clear()
        gc.collect()
        wall0 = time.perf_counter()
        cpu0 = time.process_time_ns()
        loop_cpu0 = time.thread_time_ns()
        stats = await _run_phase(svc, ins, timebase.now_ns, plan.events, args.rate, ins.lag_ns)
        t_end = time.perf_counter()
        drained = await _wait_quiescent(svc, ins, workers, warm.offered + stats.offered, args.drain_timeout)
        window = Window(
            base=base,
            final=_counts(svc, ins, workers),
            sink_puts=sink.puts - sink0,
            redis_publishes=redis.publishes - redis0,
            drained=drained,
            drain_s=time.perf_counter() - t_end,
            wall_s=time.perf_counter() - wall0,
            process_cpu_ns=time.process_time_ns() - cpu0,
            loop_cpu_ns=time.thread_time_ns() - loop_cpu0,
            warmup_offered=warm.offered,
            warmup_dropped=base["dropped"],
            committed_at_end_delta=stats.committed_at_end - base["committed"],
            idle_lag_ns=idle_lag,
        )
        svc.running = False
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task
    return stats, window


def _shard_report(svc: Any) -> list[dict[str, int]] | None:
    pipe = svc._md_shards
    if pipe is None:
        return None
    return [
        {"idx": w.idx, "offered": w.offered, "dropped": w.dropped, "rejected": w.rejected, "capacity": w.capacity}
        for w in pipe._workers
    ]


def _gil_enabled() -> bool:
    probe = getattr(sys, "_is_gil_enabled", None)
    return True if probe is None else bool(probe())


def _build_result(
    args: argparse.Namespace, plan: Plan, svc: Any, ins: Instruments, stats: ProducerStats, win: Window
) -> dict[str, Any]:
    delta = {k: win.final[k] - win.base[k] for k in win.final}
    cons = conservation(stats.offered, delta["committed"], delta["dropped"], delta["stale"], delta["rejected"])
    elapsed_s = stats.elapsed_ns / 1e9
    lag = latency_stats_us(ins.lag_ns)
    age = latency_stats_us(list(itertools.chain.from_iterable(ins.age_ns)))
    gil = _gil_enabled()
    criteria = run_criteria(
        offered=stats.offered,
        planned=stats.planned,
        rate=args.rate,
        producer_elapsed_s=elapsed_s,
        producer_error=stats.error,
        counts=delta,
        drained=win.drained,
        committed_at_end=win.committed_at_end_delta,
        lag_p99_us=lag["p99"],
        gil_enabled=gil,
        expect_gil=args.expect_gil,
    )
    committed = delta["committed"]
    producer_cpu_s = stats.cpu_ns / 1e9
    return {
        "schema": SCHEMA,
        "arm": args.arm,
        "interpreter": args.interpreter,
        "workers": args.workers,
        "rate": args.rate,
        "seconds": args.seconds,
        "repeat": args.repeat,
        "python": platform.python_version(),
        "python_build": sys.version,
        "gil_enabled": gil,
        "expect_gil": args.expect_gil,
        "rust_core_present": importlib.util.find_spec("hft_platform.rust_core") is not None,
        "cpus": list(args.cpus),
        "queue_size": args.queue_size,
        "env": {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ},
        "loadavg_start": args.loadavg_start,
        "loadavg_end": list(os.getloadavg()),
        "workload": plan.stats,
        "workload_digest": plan.digest,
        "offered": stats.offered,
        "planned": stats.planned,
        "counts": {**delta, **cons},
        "committed_at_producer_end": win.committed_at_end_delta,
        "committed_ev_s": round(win.committed_at_end_delta / elapsed_s, 1) if elapsed_s > 0 else None,
        "offered_ev_s_achieved": round(stats.offered / elapsed_s, 1) if elapsed_s > 0 else None,
        "recorder_puts": win.sink_puts,
        "redis_publishes": win.redis_publishes,
        "cpu": {
            "process_s": round(win.process_cpu_ns / 1e9, 3),
            "loop_thread_s": round(win.loop_cpu_ns / 1e9, 3),
            "producer_thread_s": round(producer_cpu_s, 3),
            "us_per_event": round((win.process_cpu_ns - stats.cpu_ns) / 1e3 / committed, 2) if committed else None,
            "us_per_event_incl_producer": round(win.process_cpu_ns / 1e3 / committed, 2) if committed else None,
            "loop_us_per_event": round(win.loop_cpu_ns / 1e3 / committed, 2) if committed else None,
        },
        "queue_age_us": age,
        "loop_probe_lag_us": lag,
        "idle_probe_lag_us": latency_stats_us(win.idle_lag_ns),
        "producer": {
            "elapsed_s": round(elapsed_s, 3),
            "late_max_us": round(stats.late_max_ns / 1e3, 1),
            "late_mean_us": round(stats.late_sum_ns / 1e3 / stats.offered, 1) if stats.offered else None,
            "late_over_1ms": stats.late_over_1ms,
            "error": stats.error,
        },
        "drained": win.drained,
        "drain_s": round(win.drain_s, 3),
        "wall_s": round(win.wall_s, 3),
        "warmup": {"offered": win.warmup_offered, "dropped": win.warmup_dropped},
        "shards": _shard_report(svc),
        "criteria": criteria,
    }


def _pin_affinity(cpus: tuple[int, ...]) -> list[int]:
    missing = set(cpus) - os.sched_getaffinity(0)
    if missing:
        raise SystemExit(f"cpus not available to this process: {sorted(missing)}")
    os.sched_setaffinity(0, cpus)
    return sorted(os.sched_getaffinity(0))


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    pinned = _pin_affinity(args.cpus)
    if pinned != list(args.cpus):
        raise RuntimeError(f"affinity is {pinned}, wanted {list(args.cpus)}")
    args.loadavg_start = list(os.getloadavg())
    os.environ["HFT_MD_WORKERS"] = str(args.workers)
    from hft_platform.utils.logging import configure_logging

    configure_logging(level=logging.WARNING)  # logs go to stderr; stdout stays clean
    spec = WorkloadSpec(n_symbols=args.symbols, bidask_fraction=args.bidask_fraction, seed=args.seed)
    plan = build_plan(spec, args.rate, args.seconds, args.warmup_per_symbol)
    svc, sink, redis = _build_service(args.workers, args.queue_size, plan)
    ins = Instruments()
    _instrument(svc, ins, args.workers)
    stats, window = asyncio.run(_drive(svc, ins, sink, redis, plan, args))
    return _build_result(args, plan, svc, ins, stats, window)


def run_command(args: argparse.Namespace) -> int:
    result = run_once(args)
    args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    crit = result["criteria"]
    print(
        f"{result['arm']} rate={result['rate']} rep={result['repeat']} sustained={crit['sustained']} "
        f"committed/s={result['committed_ev_s']} drops={result['counts']['dropped']} "
        f"lag_p99_us={result['loop_probe_lag_us']['p99']} cpu_us/ev={result['cpu']['us_per_event']}"
    )
    return 0


# -- sweep driver (parent process) -------------------------------------------------------------------------


def child_env(src_dir: Path) -> dict[str, str]:
    """A clean child environment: no inherited HFT_/SHIOAJI_/PYTHON* variables, explicit bench settings."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HFT_", "SHIOAJI_", "PYTHON"))}
    env.update(
        {
            "PYTHONPATH": str(src_dir),
            "PYTHONHASHSEED": "0",
            "PYTHONUNBUFFERED": "1",
            "HFT_MONITOR_LIVE_ENABLED": "0",
            "HFT_MD_RECORD_DIRECT": "1",
            "HFT_FEATURE_ENGINE_ENABLED": "1",
        }
    )
    return env


def child_command(interpreter: str, script: Path, arm: Arm, rate: int, repeat: int, out: Path, args: Any) -> list[str]:
    return [
        interpreter,
        str(script),
        "run",
        "--arm",
        arm.name,
        "--interpreter",
        arm.interpreter,
        "--workers",
        str(arm.workers),
        "--rate",
        str(rate),
        "--seconds",
        str(args.seconds),
        "--repeat",
        str(repeat),
        "--expect-gil",
        "off" if arm.interpreter == PY314T else "on",
        "--cpus",
        ",".join(str(c) for c in args.cpus),
        "--queue-size",
        str(args.queue_size),
        "--symbols",
        str(args.symbols),
        "--bidask-fraction",
        str(args.bidask_fraction),
        "--seed",
        str(args.seed),
        "--warmup-per-symbol",
        str(args.warmup_per_symbol),
        "--drain-timeout",
        str(args.drain_timeout),
        "--out",
        str(out),
    ]


def _failure_record(arm: Arm, rate: int, repeat: int, error: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "arm": arm.name,
        "interpreter": arm.interpreter,
        "workers": arm.workers,
        "rate": rate,
        "repeat": repeat,
        "error": error,
        "criteria": {"sustained": False},
    }


def _run_child(arm: Arm, rate: int, repeat: int, path: Path, args: Any, interpreters: dict[str, str]) -> dict:
    script = Path(__file__).resolve()
    src_dir = script.parents[2] / "src"
    cmd = child_command(interpreters[arm.interpreter], script, arm, rate, repeat, path, args)
    timeout = args.seconds + 2 * args.drain_timeout + CHILD_TIMEOUT_MARGIN_S
    try:
        proc = subprocess.run(
            cmd,
            env=child_env(src_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=src_dir.parent,
        )
    except subprocess.TimeoutExpired:
        return _failure_record(arm, rate, repeat, f"child timed out after {timeout:.0f}s")
    if proc.returncode != 0 or not path.exists():
        tail = (proc.stderr or proc.stdout or "")[-1500:]
        return _failure_record(arm, rate, repeat, f"child exit {proc.returncode}: {tail}")
    record = json.loads(path.read_text(encoding="utf-8"))
    return record if isinstance(record, dict) else _failure_record(arm, rate, repeat, "result is not an object")


def sweep_command(args: argparse.Namespace) -> int:
    interpreters = {PY312: args.py312, PY314T: args.py314t}
    needed = {arm.interpreter for arm in args.arms}
    for tag in sorted(needed):
        if not Path(interpreters[tag]).exists():
            raise SystemExit(f"interpreter for {tag} not found: {interpreters[tag]}")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    plan = [(rep, rate, arm) for rep in range(1, args.repeats + 1) for rate in args.rates for arm in args.arms]
    records = []
    for number, (rep, rate, arm) in enumerate(plan, start=1):
        path = args.out_dir / f"{arm.name}_r{rate}_rep{rep}.json"
        if args.resume and path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
        else:
            record = _run_child(arm, rate, rep, path, args, interpreters)
            if "error" in record:
                path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        records.append(record)
        _progress(number, len(plan), record)
    return _finish(records, args.repeats, args.out_dir / "summary.json")


def _progress(number: int, total: int, record: dict[str, Any]) -> None:
    if "error" in record:
        detail = f"ERROR {record['error'][:200]}"
    else:
        detail = (
            f"sustained={record['criteria']['sustained']} committed/s={record['committed_ev_s']} "
            f"drops={record['counts']['dropped']} lag_p99_us={record['loop_probe_lag_us']['p99']}"
        )
    print(f"[{number}/{total}] {record['arm']} rate={record['rate']} rep={record['repeat']} {detail}", file=sys.stderr)


def _finish(records: list[dict[str, Any]], repeats: int, out: Path | None) -> int:
    summary = summarize(records, repeats)
    gate = evaluate_gate(records, repeats=repeats)
    if out is not None:
        out.write_text(json.dumps({"summary": summary, "gate": gate}, indent=2) + "\n", encoding="utf-8")
    print(format_report(summary, gate))
    return {"PASS": 0, "FAIL": 1}.get(gate["status"], 2)


def summarize_command(args: argparse.Namespace) -> int:
    records = []
    for path in sorted(args.in_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("schema") == SCHEMA and "arm" in data:
            records.append(data)
    if not records:
        raise SystemExit(f"no run results in {args.in_dir}")
    return _finish(records, args.repeats, args.out)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"run": run_command, "sweep": sweep_command, "summarize": summarize_command}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
