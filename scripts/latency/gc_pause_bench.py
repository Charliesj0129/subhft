"""GC pause gate benchmark for the 3.12 -> free-threaded 3.14t hot-path upgrade (plan gate P1-GC).

Builds a production-scale heap, runs a tick-like allocation workload for N seconds and
measures how long the cyclic GC stops the process, in one of four arms::

    default    gc enabled, stock thresholds
    freeze     gc.collect() then gc.freeze() once the warmup is done
    threshold  gc.set_threshold(<large>) so a collection is rare (it is then long)
    disabled   gc.disable() and no collect in the window, like HFT_GC_DISABLE_TRADING

Every pause is bracketed by gc.callbacks ("start"/"stop") with time.perf_counter_ns. A
1 ms ticker thread records its largest wake-up gap, which is what another thread sees
while the collector holds the process (on 3.12 it also includes GIL hand-off time).

The heap is synthetic and stdlib only: contract-like objects, SymbolMetadata-sized
dicts, one LOB book per symbol (list-of-lists levels, the worst case for the GC) and
preallocated ring buffers. --import-runtime also imports the real runtime libraries so
their objects are part of the heap. Prices are scaled ints (x10000).

The pre-registered stop limit is p99 <= 1 ms and max <= 5 ms in the chosen arm. The
window numbers say what the workload paid; "full_collect_probe_ns" says what one full
collection of this heap costs when the window happened not to trigger one, and
"post_run_full_collect_ns" is the collect a disabled arm still owes. Exit code: 0 when
the gate passes, 1 when it fails (argument errors exit 2).

Usage::

    uv run python scripts/latency/gc_pause_bench.py --mode freeze --seconds 20
"""

from __future__ import annotations

import argparse
import gc
import importlib
import json
import os
import sys
import threading
import time

MODES = ("default", "freeze", "threshold", "disabled")
P99_LIMIT_NS = 1_000_000
MAX_LIMIT_NS = 5_000_000
BASE_PRICE_X10000 = 1_000_000
TICK_SIZE_X10000 = 100
RUNTIME_LIBS = (
    "numpy",
    "pandas",
    "clickhouse_connect",
    "structlog",
    "msgspec",
    "shioaji",
    "prometheus_client",
    "psutil",
    "yaml",
    "numba",
    "scipy",
    "exchange_calendars",
    "aiohttp",
)


class FakeContract:
    """Contract-like object: a Python instance with about twenty attributes plus a set and a dict."""

    def __init__(self, index: int) -> None:
        code = f"TXO{index:08d}"
        self.code = code
        self.symbol = code
        self.name = f"contract-{index}"
        self.category = "TXO"
        self.exchange = "TAIFEX"
        self.security_type = "OPT"
        self.currency = "TWD"
        self.delivery_month = "202612"
        self.delivery_date = "2026/12/16"
        self.strike_price = 20_000 + (index % 400) * 50
        self.option_right = "C" if index & 1 else "P"
        self.underlying_kind = "I"
        self.underlying_code = "TXF"
        self.limit_up = 31_000_0000
        self.limit_down = 9_000_0000
        self.reference = 20_000_0000
        self.multiplier = 50
        self.unit = 1
        self.update_date = "2026/11/23"
        self.fields_set = {"code", "symbol", "name"}
        self.extra = {"tags": ("txo", "weekly"), "index": index}


class FakeBook:
    """Per-symbol LOB state with the BookState slot layout."""

    __slots__ = (
        "symbol",
        "bids",
        "asks",
        "exch_ts",
        "local_ts",
        "version",
        "mid_price_x2",
        "spread",
        "last_price",
        "last_volume",
        "bid_depth_total",
        "ask_depth_total",
    )

    def __init__(self, symbol: str, depth: int) -> None:
        self.symbol = symbol
        self.bids = [[BASE_PRICE_X10000 - TICK_SIZE_X10000 * (d + 1), 10 + d] for d in range(depth)]
        self.asks = [[BASE_PRICE_X10000 + TICK_SIZE_X10000 * (d + 1), 10 + d] for d in range(depth)]
        self.exch_ts = 0
        self.local_ts = 0
        self.version = 0
        self.mid_price_x2 = 2 * BASE_PRICE_X10000
        self.spread = 2 * TICK_SIZE_X10000
        self.last_price = BASE_PRICE_X10000
        self.last_volume = 0
        self.bid_depth_total = 0
        self.ask_depth_total = 0


class Heap:
    """Everything the benchmark keeps alive for the whole run."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.depth = args.depth
        self.ring_size = args.ring_size
        self.cycle_every = args.cycle_every
        self.retain_every = args.retain_every
        self.symbols = [f"S{n:04d}" for n in range(args.symbols)]
        self.contracts = [FakeContract(n) for n in range(args.contracts)]
        self.by_code = {c.code: c for c in self.contracts}
        self.meta = {
            s: {
                "code": s,
                "name": f"name-{s}",
                "exchange": "TSE",
                "product_type": "stock",
                "price_scale": 10_000,
                "tick_size": TICK_SIZE_X10000,
                "tags": ["core", "liquid"],
                "lot": 1000,
            }
            for s in self.symbols
        }
        self.tags_by_symbol = {s: {"core", "liquid"} for s in self.symbols}
        self.symbols_by_tag = {"core": set(self.symbols), "liquid": set(self.symbols)}
        self.exchange_cache = {s: "TSE" for s in self.symbols}
        self.books = [FakeBook(s, args.depth) for s in self.symbols]
        self.tick_ring: list[tuple[object, ...] | None] = [None] * args.ring_size
        self.stats_ring: list[tuple[object, ...] | None] = [None] * args.ring_size
        self.kind_ring = [0] * args.ring_size
        self.retained: list[dict[str, object] | None] = [None] * args.retain_size
        self.ballast = [[n] for n in range(args.ballast)]


class PauseRecorder:
    """gc.callbacks hook that records start-to-stop time of every collection while active."""

    def __init__(self) -> None:
        self.active = False
        self.pauses_ns: list[int] = []
        self.generations: list[int] = []
        self._start_ns = 0

    def __call__(self, phase: str, info: dict[str, int]) -> None:
        now = time.perf_counter_ns()
        if phase == "start":
            self._start_ns = now
        elif self.active:
            self.pauses_ns.append(now - self._start_ns)
            self.generations.append(info.get("generation", -1))


class Ticker:
    """A thread that sleeps 1 ms in a loop and keeps the largest gap between wake-ups."""

    def __init__(self) -> None:
        self.max_gap_ns = 0
        self.gaps_over_5ms = 0
        self.wakeups = 0
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="gc-bench-ticker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop = True
        self._thread.join()

    def _run(self) -> None:
        last = time.perf_counter_ns()
        while not self._stop:
            time.sleep(0.001)
            now = time.perf_counter_ns()
            gap = now - last
            last = now
            self.wakeups += 1
            if gap > self.max_gap_ns:
                self.max_gap_ns = gap
            if gap > 5_000_000:
                self.gaps_over_5ms += 1


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def non_negative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {value}")
    return value


def positive_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=MODES, default="default", help="GC arm (default: %(default)s)")
    parser.add_argument("--seconds", type=positive_float, default=20.0, help="measured window length")
    parser.add_argument("--contracts", type=non_negative_int, default=54_000, help="contract-like objects")
    parser.add_argument("--symbols", type=positive_int, default=2_900, help="SymbolMetadata entries and LOB books")
    parser.add_argument("--depth", type=positive_int, default=5, help="LOB levels per side")
    parser.add_argument("--ring-size", type=positive_int, default=65_536, help="slots per ring buffer")
    parser.add_argument("--retain-size", type=positive_int, default=100_000, help="long-lived record ring slots")
    parser.add_argument(
        "--retain-every", type=positive_int, default=50, help="every Nth tick stores a long-lived record"
    )
    parser.add_argument("--cycle-every", type=positive_int, default=20, help="every Nth tick leaves a 3-object cycle")
    parser.add_argument(
        "--ballast", type=non_negative_int, default=0, help="extra GC-tracked objects to calibrate heap size"
    )
    parser.add_argument("--tick-rate", type=non_negative_int, default=50_000, help="ticks per second; 0 = unpaced")
    parser.add_argument(
        "--warmup-ticks", type=non_negative_int, default=200_000, help="ticks before the arm is applied"
    )
    parser.add_argument("--threshold", type=positive_int, default=1_000_000, help="threshold0 for --mode threshold")
    parser.add_argument("--probe-collections", type=non_negative_int, default=5, help="explicit full collections timed")
    parser.add_argument("--p99-limit-ns", type=positive_int, default=P99_LIMIT_NS, help="pre-registered p99 limit")
    parser.add_argument("--max-limit-ns", type=positive_int, default=MAX_LIMIT_NS, help="pre-registered max limit")
    parser.add_argument("--import-runtime", action="store_true", help="import the real runtime libraries into the heap")
    return parser


def percentile_ns(sorted_values: list[int], pct: int) -> int:
    """Nearest-rank percentile of an ascending list; 0 for an empty list."""
    if not sorted_values:
        return 0
    rank = (len(sorted_values) * pct + 99) // 100
    return sorted_values[max(rank, 1) - 1]


def evaluate_gate(pauses_ns: list[int], p99_limit_ns: int, max_limit_ns: int) -> dict[str, object]:
    ordered = sorted(pauses_ns)
    p99 = percentile_ns(ordered, 99)
    worst = ordered[-1] if ordered else 0
    p99_ok = p99 <= p99_limit_ns
    max_ok = worst <= max_limit_ns
    return {
        "p99_limit_ns": p99_limit_ns,
        "max_limit_ns": max_limit_ns,
        "p99_ok": p99_ok,
        "max_ok": max_ok,
        "passed": p99_ok and max_ok,
    }


def rss_kb() -> int:
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            pages = int(handle.read().split()[1])
    except (OSError, ValueError, IndexError):
        return -1
    return pages * (os.sysconf("SC_PAGE_SIZE") // 1024)


def gil_enabled() -> bool:
    probe = getattr(sys, "_is_gil_enabled", None)
    return True if probe is None else bool(probe())


def import_runtime_libs() -> dict[str, str]:
    """Import the runtime libraries; return {module: "ok" or the error}."""
    outcome = {}
    for name in RUNTIME_LIBS:
        try:
            importlib.import_module(name)
            outcome[name] = "ok"
        except ImportError as exc:
            outcome[name] = f"ImportError: {exc}"
    return outcome


def run_ticks(heap: Heap, start: int, count: int) -> int:
    """Process count ticks from sequence start and return the next sequence number."""
    books = heap.books
    symbols = heap.symbols
    n_sym = len(books)
    ring = heap.ring_size
    tick_ring = heap.tick_ring
    stats_ring = heap.stats_ring
    retained = heap.retained
    retain_len = len(retained)
    levels = range(heap.depth)
    depth_total = 10 * heap.depth
    cycle_every = heap.cycle_every
    retain_every = heap.retain_every
    for i in range(start, start + count):
        sym = (i * 7919) % n_sym
        price = BASE_PRICE_X10000 + (sym % 500) * TICK_SIZE_X10000 + (i % 21 - 10) * TICK_SIZE_X10000
        symbol = symbols[sym]
        book = books[sym]
        tick_ring[i % ring] = (symbol, price, 1 + i % 5, i, False, False, i)
        book.bids = [[price - TICK_SIZE_X10000 * (d + 1), 10 + d] for d in levels]
        book.asks = [[price + TICK_SIZE_X10000 * (d + 1), 10 + d] for d in levels]
        book.exch_ts = i
        book.version += 1
        book.last_price = price
        book.mid_price_x2 = 2 * price
        stats_ring[i % ring] = ("lobstats", symbol, price - TICK_SIZE_X10000, price + TICK_SIZE_X10000, depth_total, i)
        if i % cycle_every == 0:
            first: dict[str, object] = {"i": i}
            second: list[object] = [first]
            first["peer"] = second
            second.append({"back": first})
        if i % retain_every == 0:
            retained[(i // retain_every) % retain_len] = {
                "id": i,
                "legs": [price, price + TICK_SIZE_X10000],
                "state": "W",
            }
    return start + count


def run_window(heap: Heap, start: int, seconds: float, tick_rate: int) -> tuple[int, int]:
    """Run the workload for seconds; return (next sequence number, ticks processed)."""
    end_ns = time.perf_counter_ns() + int(seconds * 1_000_000_000)
    seq = start
    if tick_rate == 0:
        while time.perf_counter_ns() < end_ns:
            seq = run_ticks(heap, seq, 256)
        return seq, seq - start
    batch = max(1, tick_rate // 1000)
    period_ns = batch * 1_000_000_000 // tick_rate
    due_ns = time.perf_counter_ns()
    while due_ns < end_ns:
        seq = run_ticks(heap, seq, batch)
        due_ns += period_ns
        slack_ns = due_ns - time.perf_counter_ns()
        if slack_ns > 0:
            time.sleep(slack_ns / 1_000_000_000)
    return seq, seq - start


def apply_mode(mode: str, threshold0: int) -> dict[str, object]:
    """Put the GC into the arm. Always starts from a freshly collected heap."""
    gc.collect()
    if mode == "freeze":
        gc.freeze()
    elif mode == "threshold":
        current = gc.get_threshold()
        gc.set_threshold(threshold0, current[1], current[2])
    elif mode == "disabled":
        gc.disable()
    return {
        "threshold": list(gc.get_threshold()),
        "enabled": gc.isenabled(),
        "frozen_objects": gc.get_freeze_count(),
    }


def time_full_collections(count: int) -> dict[str, int]:
    samples = []
    for _ in range(count):
        start = time.perf_counter_ns()
        gc.collect()
        samples.append(time.perf_counter_ns() - start)
    ordered = sorted(samples)
    return {"count": count, "p50": percentile_ns(ordered, 50), "max": ordered[-1] if ordered else 0}


def by_generation(recorder: PauseRecorder) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for generation, pause in zip(recorder.generations, recorder.pauses_ns, strict=True):
        slot = summary.setdefault(str(generation), {"count": 0, "max_ns": 0})
        slot["count"] += 1
        slot["max_ns"] = max(slot["max_ns"], pause)
    return summary


def run_bench(args: argparse.Namespace) -> dict[str, object]:
    """Build the heap, run the arm and return the JSON-able report. Restores the GC state afterwards."""
    was_enabled = gc.isenabled()
    threshold_before = gc.get_threshold()
    recorder = PauseRecorder()
    ticker = Ticker()
    try:
        imported = import_runtime_libs() if args.import_runtime else {}
        heap = Heap(args)
        gc.collect()
        tracked_objects = len(gc.get_objects())
        rss_after_heap = rss_kb()
        seq = run_ticks(heap, 0, args.warmup_ticks)
        gc_state = apply_mode(args.mode, args.threshold)
        probe = time_full_collections(args.probe_collections)
        rss_window_start = rss_kb()
        gc.callbacks.append(recorder)
        ticker.start()
        recorder.active = True
        seq, ticks = run_window(heap, seq, args.seconds, args.tick_rate)
        recorder.active = False
        ticker.stop()
        gc.callbacks.remove(recorder)
        rss_window_end = rss_kb()
        post_start = time.perf_counter_ns()
        post_collected = gc.collect()
        post_ns = time.perf_counter_ns() - post_start
    finally:
        recorder.active = False
        if recorder in gc.callbacks:
            gc.callbacks.remove(recorder)
        if args.mode == "freeze":
            gc.unfreeze()
        gc.set_threshold(*threshold_before)
        if was_enabled:
            gc.enable()
    ordered = sorted(recorder.pauses_ns)
    return {
        "python_version": sys.version.split()[0],
        "gil_enabled": gil_enabled(),
        "mode": args.mode,
        "seconds": args.seconds,
        "pause_count": len(ordered),
        "pause_p50_ns": percentile_ns(ordered, 50),
        "pause_p99_ns": percentile_ns(ordered, 99),
        "pause_max_ns": ordered[-1] if ordered else 0,
        "pause_total_ns": sum(ordered),
        "pause_by_generation": by_generation(recorder),
        "ticker_max_gap_ns": ticker.max_gap_ns,
        "ticker_gaps_over_5ms": ticker.gaps_over_5ms,
        "ticker_wakeups": ticker.wakeups,
        "ticks_processed": ticks,
        "achieved_ticks_per_s": int(ticks / args.seconds),
        "gate": evaluate_gate(recorder.pauses_ns, args.p99_limit_ns, args.max_limit_ns),
        "full_collect_probe_ns": probe,
        "post_run_full_collect_ns": post_ns,
        "post_run_collected_objects": post_collected,
        "gc": {"threshold_before": list(threshold_before), **gc_state},
        "heap": {
            "tracked_objects": tracked_objects,
            "contracts": args.contracts,
            "symbols": args.symbols,
            "lob_depth": args.depth,
            "ring_size": args.ring_size,
            "retain_size": args.retain_size,
            "ballast": args.ballast,
            "imported_runtime": imported,
        },
        "workload": {
            "tick_rate": args.tick_rate,
            "cycle_every": args.cycle_every,
            "retain_every": args.retain_every,
            "warmup_ticks": args.warmup_ticks,
        },
        "rss_kb": {"after_heap": rss_after_heap, "window_start": rss_window_start, "window_end": rss_window_end},
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_bench(args)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
