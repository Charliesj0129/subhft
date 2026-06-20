"""Offline Decimal-vs-float normalize bench for the shioaji 1.5.3 upgrade (#367).

Shioaji 1.5.3 callbacks deliver tick/bidask prices as ``decimal.Decimal``; 1.3.3
delivered ``float``. This bench measures whether routing Decimal prices through
the real ``MarketDataNormalizer.normalize_tick`` / ``normalize_bidask`` paths
costs materially more than the legacy float payloads, and whether the in-tree
Rust kernels consume Decimal directly (fallback rate == 0) or silently fall back
to the pure-Python path (a perf regression, not a correctness one — the boundary
test proves the *value* is identical either way).

SDK-free by design (no ``import shioaji``): it runs in BOTH the project ``.venv``
(1.3.3) and the throwaway ``shioaji[speed]==1.5.3`` harness venv and yields the
same numbers there, because numpy/pydantic are unchanged across the two venvs
(see the Phase-0 freeze delta). The point measured is the Decimal arithmetic at
the normalizer boundary, which is purely a function of the platform code.

Gates are deliberately RELATIVE (machine-independent), not absolute wall-clock
maxima that drift per host:

  * ``rust_fallback_rate == 0.0`` — Decimal must be consumed by the Rust kernel,
    never pervasively fall back. (A nonzero rate is a CONDITIONAL-only perf flag,
    not a correctness block — values stay correct via the Python fallback.)
  * ``decimal_us_per_event <= float_us_per_event * RATIO_MAX`` for tick and
    bidask — the Decimal path must not be dramatically slower than float.

Run:
    PYTHONPATH=src python tests/benchmark/bench_shioaji_normalize_decimal.py --check
    PYTHONPATH=src python tests/benchmark/bench_shioaji_normalize_decimal.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import tracemalloc
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
for _p in (str(REPO_ROOT), str(SRC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import hft_platform.feed_adapter.normalizer as normalizer_mod  # noqa: E402
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer  # noqa: E402

_SCALE = 10_000
# The Decimal path must stay within this multiple of the float path. Decimal
# arithmetic is inherently a few× slower than float in pure Python, but the
# in-tree Rust kernel extracts Decimal directly, so on the Rust path the gap is
# small; this ceiling catches a regression to a slow pervasive fallback.
_RATIO_MAX = 3.0
_FALLBACK_RATE_MAX = 0.0

# Representative tick-grid prices (no sub-tick ties — those are a correctness
# concern handled by the boundary test, not a perf one).
_PRICES = [580.0, 579.5, 17350.0, 123.45, 1.23, 9999.99, 42.0, 678.0]

# A near-now base timestamp so the normalizer's stale-feed skew clamp does not
# fire (it would log a warning and is irrelevant to the arithmetic being timed).
# Acceptable in an offline bench harness — this is not a hot-path code site.
_BASE_TS_NS = time.time_ns()


def _make_normalizer() -> tuple[MarketDataNormalizer, Any]:
    """Build a normalizer over one stock + one future, both scaled x10000."""
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    tmp.write(
        "symbols:\n"
        "  - code: '2330'\n"
        "    exchange: 'TSE'\n"
        "    price_scale: 10000\n"
        "  - code: 'TXFR1'\n"
        "    exchange: 'TAIFEX'\n"
        "    price_scale: 10000\n"
    )
    tmp.flush()
    tmp.close()
    return MarketDataNormalizer(tmp.name), tmp.name


def _tick_payload(price: Any, i: int) -> dict[str, Any]:
    return {
        "code": "2330",
        "close": price,
        "volume": 10 + (i % 7),
        "total_volume": 1000 + i,
        "ts": _BASE_TS_NS + i,
        "simtrade": 0,
        "intraday_odd": 0,
    }


def _bidask_payload(prices: list[Any], i: int) -> dict[str, Any]:
    bid = prices[:5]
    ask = [p for p in prices[3:8]]
    return {
        "code": "2330",
        "ts": _BASE_TS_NS + i,
        "bid_price": bid,
        "bid_volume": [1, 2, 3, 4, 5][: len(bid)],
        "ask_price": ask,
        "ask_volume": [5, 4, 3, 2, 1][: len(ask)],
    }


def _as_decimal(x: float) -> Decimal:
    return Decimal(str(x))


def _counter(child: Any) -> float:
    try:
        return float(child._value.get())  # type: ignore[attr-defined]
    except Exception:
        return 0.0


def _bench_path(norm: MarketDataNormalizer, kind: str, decimal_prices: bool, iters: int) -> dict[str, Any]:
    """Time `iters` normalize calls of `kind` ('tick'|'bidask') and count fallbacks."""
    conv = _as_decimal if decimal_prices else (lambda x: x)
    if kind == "tick":
        payloads = [_tick_payload(conv(_PRICES[i % len(_PRICES)]), i) for i in range(iters)]
        fn = norm.normalize_tick
        fb_before = _counter(norm._rust_fallback_tick)
    else:
        payloads = [_bidask_payload([conv(p) for p in _PRICES], i) for i in range(iters)]
        fn = norm.normalize_bidask
        fb_before = _counter(norm._rust_fallback_bidask)

    emitted = 0
    t0 = time.perf_counter()
    for p in payloads:
        if fn(p) is not None:
            emitted += 1
    elapsed = time.perf_counter() - t0

    fb_after = _counter(norm._rust_fallback_tick if kind == "tick" else norm._rust_fallback_bidask)
    return {
        "kind": kind,
        "price_type": "decimal" if decimal_prices else "float",
        "iters": iters,
        "emitted": emitted,
        "us_per_event": (elapsed / iters) * 1e6,
        "fallbacks": fb_after - fb_before,
        "fallback_rate": (fb_after - fb_before) / iters if iters else 0.0,
    }


def run(iters: int) -> dict[str, Any]:
    norm, cfg_path = _make_normalizer()
    try:
        # Warm up (JIT-free, but primes caches / metric children).
        _bench_path(norm, "tick", False, min(1000, iters))
        _bench_path(norm, "bidask", False, min(1000, iters))

        tracemalloc.start()
        results = {
            "tick_float": _bench_path(norm, "tick", False, iters),
            "tick_decimal": _bench_path(norm, "tick", True, iters),
            "bidask_float": _bench_path(norm, "bidask", False, iters),
            "bidask_decimal": _bench_path(norm, "bidask", True, iters),
        }
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    finally:
        Path(cfg_path).unlink(missing_ok=True)

    tick_ratio = results["tick_decimal"]["us_per_event"] / max(results["tick_float"]["us_per_event"], 1e-9)
    bidask_ratio = results["bidask_decimal"]["us_per_event"] / max(results["bidask_float"]["us_per_event"], 1e-9)
    max_fallback_rate = max(
        results["tick_decimal"]["fallback_rate"], results["bidask_decimal"]["fallback_rate"]
    )
    return {
        "rust_enabled": normalizer_mod._RUST_ENABLED,
        "iters": iters,
        "tracemalloc_peak_bytes": peak,
        "tick_decimal_vs_float_ratio": tick_ratio,
        "bidask_decimal_vs_float_ratio": bidask_ratio,
        "max_rust_fallback_rate": max_fallback_rate,
        "ratio_max": _RATIO_MAX,
        "fallback_rate_max": _FALLBACK_RATE_MAX,
        "results": results,
    }


def _check(summary: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if summary["max_rust_fallback_rate"] > _FALLBACK_RATE_MAX:
        failures.append(
            f"rust_fallback_rate {summary['max_rust_fallback_rate']:.4f} > {_FALLBACK_RATE_MAX} "
            "(Decimal is not being consumed by the Rust kernel — pervasive fallback)"
        )
    if summary["tick_decimal_vs_float_ratio"] > _RATIO_MAX:
        failures.append(
            f"tick decimal/float us ratio {summary['tick_decimal_vs_float_ratio']:.2f} > {_RATIO_MAX}"
        )
    if summary["bidask_decimal_vs_float_ratio"] > _RATIO_MAX:
        failures.append(
            f"bidask decimal/float us ratio {summary['bidask_decimal_vs_float_ratio']:.2f} > {_RATIO_MAX}"
        )
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--iters", type=int, default=50_000)
    ap.add_argument("--json", action="store_true", help="emit the summary as JSON")
    ap.add_argument("--check", action="store_true", help="enforce relative gates; nonzero exit on failure")
    args = ap.parse_args()

    summary = run(args.iters)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        r = summary["results"]
        print(f"rust_enabled={summary['rust_enabled']} iters={summary['iters']}")
        for key in ("tick_float", "tick_decimal", "bidask_float", "bidask_decimal"):
            row = r[key]
            print(
                f"  {key:14s} {row['us_per_event']:8.3f} us/event  "
                f"fallback_rate={row['fallback_rate']:.4f}  emitted={row['emitted']}"
            )
        print(f"  tick   decimal/float ratio = {summary['tick_decimal_vs_float_ratio']:.2f} (max {_RATIO_MAX})")
        print(f"  bidask decimal/float ratio = {summary['bidask_decimal_vs_float_ratio']:.2f} (max {_RATIO_MAX})")
        print(f"  max rust fallback rate     = {summary['max_rust_fallback_rate']:.4f} (max {_FALLBACK_RATE_MAX})")
        print(f"  tracemalloc peak           = {summary['tracemalloc_peak_bytes'] / 1024:.1f} KiB")

    if args.check:
        failures = _check(summary)
        if failures:
            print("FAIL bench_shioaji_normalize_decimal:", file=sys.stderr)
            for f in failures:
                print(f"  - {f}", file=sys.stderr)
            return 1
        print("OK bench_shioaji_normalize_decimal: fallback-rate 0, decimal path within ratio bound")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
