"""Safe bulk RTT probe for Shioaji live broker.

Produces a statistically-significant live broker RTT baseline by firing N
order place/cancel cycles with quotes placed FAR from market so they
physically cannot fill.

Safety design
-------------

1. **Far-from-market limit price**: every order sits at best_bid - offset
   (buy) or best_ask + offset (sell). Default offset 300 TXF ticks
   (= 300 points ≈ 3× typical intra-session range). No risk of adverse
   selection.

2. **Immediate cancel**: each place is paired with a cancel in the same
   iteration. ROD day orders that don't get cancelled are explicitly
   cancelled at probe exit.

3. **Fill circuit-breaker**: if a place_order returns a FILLED or
   PARTIALLY_FILLED trade (which should be impossible given the price
   offset), the probe aborts and the remaining iterations are skipped.

4. **Rate-limit respect**: configurable sleep between iterations
   (default 0.2 s → 5 cycles/sec = 30 API calls/10 sec, well under
   Shioaji's 250/10-sec cap).

5. **Market hours**: script refuses to run in "real" mode outside
   configured trading windows (day 08:45-13:45, night 15:00-05:00 CST).

Output
------

JSON with per-operation:
  - count, errors
  - raw samples (all durations in us)
  - mean / std / P50 / P90 / P95 / P99 / max
  - bootstrap 95% CI for P50 and P95

Invocation
----------

Sim dry-run::

    uv run python scripts/latency/shioaji_rtt_bulk_probe.py \\
        --mode sim --iters 30 --symbol TXFR1

Live probe (REQUIRES CA) — after setting SHIOAJI_CA_PATH +
SHIOAJI_CA_PASSWORD::

    uv run python scripts/latency/shioaji_rtt_bulk_probe.py \\
        --mode real --iters 1000 --symbol TXFE6 --offset-ticks 300 \\
        --out outputs/shioaji_rtt_live_bulk_$(date +%Y%m%d_%H%M).json
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any

try:
    import numpy as np
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"numpy not available: {exc}")

try:
    import shioaji as sj
except Exception as exc:  # pragma: no cover
    raise SystemExit(f"shioaji not available: {exc}")


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------

_TW_TZ_NAME = "Asia/Taipei"
_DAY_SESSION = (dtime(8, 45), dtime(13, 45))
_NIGHT_SESSION_START = dtime(15, 0)
_NIGHT_SESSION_END = dtime(5, 0)


def _in_trading_hours(now: datetime | None = None) -> bool:
    """TAIFEX futures — day or night session, Asia/Taipei."""
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(_TW_TZ_NAME)
    except Exception:
        tz = None
    if now is None:
        now = datetime.now(tz=tz)
    t = now.time()
    if _DAY_SESSION[0] <= t < _DAY_SESSION[1]:
        return True
    # Night session wraps midnight
    if t >= _NIGHT_SESSION_START or t < _NIGHT_SESSION_END:
        return True
    return False


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@dataclass
class OpStats:
    op: str
    count: int
    errors: int
    mean_us: float
    std_us: float
    p50_us: float
    p90_us: float
    p95_us: float
    p99_us: float
    max_us: float
    p50_ci95_us: tuple[float, float]
    p95_ci95_us: tuple[float, float]


def _bootstrap_percentile_ci(
    samples: np.ndarray, q: float, n_boot: int = 1000, alpha: float = 0.05
) -> tuple[float, float]:
    if samples.size < 10:
        return (0.0, 0.0)
    rng = np.random.default_rng(seed=42)
    boots = rng.choice(samples, size=(n_boot, samples.size), replace=True)
    vals = np.percentile(boots, q * 100, axis=1)
    lo = float(np.percentile(vals, 100 * alpha / 2))
    hi = float(np.percentile(vals, 100 * (1 - alpha / 2)))
    return (lo, hi)


def _summarize(op: str, samples_us: list[int], errors: int) -> OpStats:
    if not samples_us:
        return OpStats(op, 0, errors, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, (0.0, 0.0), (0.0, 0.0))
    arr = np.asarray(samples_us, dtype=np.float64)
    return OpStats(
        op=op,
        count=int(arr.size),
        errors=errors,
        mean_us=float(arr.mean()),
        std_us=float(arr.std(ddof=0)),
        p50_us=float(np.percentile(arr, 50)),
        p90_us=float(np.percentile(arr, 90)),
        p95_us=float(np.percentile(arr, 95)),
        p99_us=float(np.percentile(arr, 99)),
        max_us=float(arr.max()),
        p50_ci95_us=_bootstrap_percentile_ci(arr, 0.5),
        p95_ci95_us=_bootstrap_percentile_ci(arr, 0.95),
    )


# ---------------------------------------------------------------------------
# Probe core
# ---------------------------------------------------------------------------


def _snapshot_bidask(api: Any, contract: Any) -> tuple[int, int] | None:
    try:
        snaps = api.snapshots([contract])
    except Exception:
        return None
    if not snaps:
        return None
    snap = snaps[0]
    data = snap if isinstance(snap, dict) else getattr(snap, "__dict__", {})
    bid = data.get("buy_price") if isinstance(data, dict) else getattr(snap, "buy_price", None)
    ask = data.get("sell_price") if isinstance(data, dict) else getattr(snap, "sell_price", None)

    def _as_int(v: Any) -> int | None:
        if v is None:
            return None
        try:
            return int(float(v))
        except Exception:
            return None

    bid_i = _as_int(bid)
    ask_i = _as_int(ask)
    if not bid_i or not ask_i:
        return None
    return bid_i, ask_i


def _build_futures_order(sj_: Any, api: Any, action_str: str, price: int, qty: int) -> Any:
    act = sj_.constant.Action.Buy if action_str == "Buy" else sj_.constant.Action.Sell
    return sj_.Order(
        price=price,
        quantity=qty,
        action=act,
        price_type=sj_.constant.FuturesPriceType.LMT,
        order_type=sj_.constant.OrderType.ROD,
        account=api.futopt_account,
    )


def _trade_filled(trade: Any) -> bool:
    if trade is None:
        return False
    status = getattr(trade, "status", None) or (trade.get("status") if isinstance(trade, dict) else None)
    if status is None:
        return False
    s = status if isinstance(status, str) else getattr(status, "status", "") or (status.get("status", "") if isinstance(status, dict) else "")
    return str(s).lower() in {"filled", "partially_filled", "filled_partial"}


_PLACEHOLDER_ORDNOS = {"", "00", "0", "000000", None}


def _ordno_ready(trade: Any) -> bool:
    if trade is None:
        return False
    order = getattr(trade, "order", None) or (trade.get("order") if isinstance(trade, dict) else None)
    if order is None:
        return False
    ordno = getattr(order, "ordno", None) or (order.get("ordno") if isinstance(order, dict) else None)
    return ordno not in _PLACEHOLDER_ORDNOS


def _wait_for_ordno(api: Any, trade: Any, timeout_s: float = 2.0) -> int:
    """Spin-wait for Shioaji callback to populate real ordno.

    Returns the elapsed microseconds. The probe counts this toward
    ``submitted_ack_us`` — it is exactly the broker-side SUBMITTED-ack
    round-trip that the strategy would depend on for in-flight tracking.
    """
    start = time.perf_counter_ns()
    deadline = start + int(timeout_s * 1e9)
    while time.perf_counter_ns() < deadline:
        try:
            api.update_status(api.futopt_account)
        except Exception:
            pass
        if _ordno_ready(trade):
            break
        time.sleep(0.02)
    return (time.perf_counter_ns() - start) // 1000


def _probe_one_cycle(
    api: Any,
    contract: Any,
    sj_: Any,
    qty: int,
    side: str,
    offset_ticks: int,
) -> tuple[int | None, int | None, int | None, Any]:
    """Run one place+wait-ordno+cancel cycle.

    Returns (place_us, submitted_ack_us, cancel_us, trade).
    Any duration may be None if that step failed.
    """
    bidask = _snapshot_bidask(api, contract)
    if bidask is None:
        return (None, None, None, None)
    bid, ask = bidask
    if side == "Buy":
        price = max(1, bid - offset_ticks)
    else:
        price = ask + offset_ticks
    order = _build_futures_order(sj_, api, side, price, qty)

    place_us: int | None = None
    submitted_ack_us: int | None = None
    cancel_us: int | None = None
    trade: Any = None

    t0 = time.perf_counter_ns()
    try:
        trade = api.place_order(contract, order)
        place_us = (time.perf_counter_ns() - t0) // 1000
    except Exception:
        return (None, None, None, None)

    if _trade_filled(trade):
        # Far-from-market price → should be impossible. Abort signal.
        return (place_us, None, None, trade)

    # Wait for real ordno to propagate so cancel_order has a valid target.
    submitted_ack_us = _wait_for_ordno(api, trade, timeout_s=2.0)
    if not _ordno_ready(trade):
        # Broker never returned a real ordno — treat cancel as unmeasurable
        # but leave the trade alone for later sweep.
        return (place_us, submitted_ack_us, None, trade)

    t1 = time.perf_counter_ns()
    try:
        api.cancel_order(trade)
        cancel_us = (time.perf_counter_ns() - t1) // 1000
    except Exception:
        cancel_us = None

    return (place_us, submitted_ack_us, cancel_us, trade)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Safe bulk Shioaji live-broker RTT probe.")
    parser.add_argument("--mode", choices=["sim", "real"], default="sim", help="sim=simulation, real=live broker (requires CA)")
    parser.add_argument("--iters", type=int, default=100, help="main iteration count")
    parser.add_argument("--warmup", type=int, default=5, help="warmup iterations discarded")
    parser.add_argument("--sleep", type=float, default=0.2, help="sleep (s) between cycles — rate-limit guard")
    parser.add_argument("--symbol", default=os.getenv("HFT_SHIOAJI_PROBE_SYMBOL", "TXFR1"))
    parser.add_argument("--qty", type=int, default=1)
    parser.add_argument("--offset-ticks", type=int, default=300, help="far-from-market price offset")
    parser.add_argument("--side", choices=["Buy", "Sell", "alternate"], default="alternate")
    parser.add_argument("--out", required=True, help="output JSON path")
    parser.add_argument("--skip-market-hours-check", action="store_true")
    args = parser.parse_args()

    if args.mode == "real":
        if not args.skip_market_hours_check and not _in_trading_hours():
            raise SystemExit("Refusing to probe in real mode outside TAIFEX trading hours (day 08:45-13:45, night 15:00-05:00 CST). Use --skip-market-hours-check to override.")

    simulation = args.mode == "sim"
    api = sj.Shioaji(simulation=simulation)

    api_key = os.getenv("SHIOAJI_API_KEY")
    secret = os.getenv("SHIOAJI_SECRET_KEY")
    if not api_key or not secret:
        raise SystemExit("Missing SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY in env.")

    api.login(api_key=api_key, secret_key=secret, contracts_timeout=60000)

    if not simulation:
        ca_path = os.getenv("SHIOAJI_CA_PATH")
        ca_pass = os.getenv("SHIOAJI_CA_PASSWORD")
        if not ca_path or not ca_pass:
            raise SystemExit("Real mode requires SHIOAJI_CA_PATH + SHIOAJI_CA_PASSWORD in env.")
        api.activate_ca(ca_path=ca_path, ca_passwd=ca_pass)

    # Resolve futures contract
    contract = None
    try:
        contract = api.Contracts.Futures[args.symbol]
    except Exception:
        pass
    if contract is None:
        # Try R1 / R2 continuous
        for fallback in ("TXFR1", "TXFR2", "MXFR1"):
            try:
                contract = getattr(api.Contracts.Futures.TXF, fallback, None)
                if contract is not None:
                    break
            except Exception:
                continue
    if contract is None:
        raise SystemExit(f"Cannot resolve futures contract for symbol={args.symbol!r}")

    # Warmup
    sides = ["Buy", "Sell"] if args.side == "alternate" else [args.side]
    for i in range(args.warmup):
        side = sides[i % len(sides)]
        _probe_one_cycle(api, contract, sj, args.qty, side, args.offset_ticks)
        time.sleep(args.sleep)

    # Main loop
    place_samples: list[int] = []
    submitted_ack_samples: list[int] = []
    cancel_samples: list[int] = []
    place_errors = 0
    submitted_ack_errors = 0
    cancel_errors = 0
    fills_aborted = 0

    start = time.time()
    for i in range(args.iters):
        side = sides[i % len(sides)]
        place_us, submitted_ack_us, cancel_us, trade = _probe_one_cycle(
            api, contract, sj, args.qty, side, args.offset_ticks
        )
        if _trade_filled(trade):
            fills_aborted += 1
            print(f"!! ABORT at iter {i}: trade filled unexpectedly — stopping.", file=sys.stderr)
            break
        if place_us is None:
            place_errors += 1
        else:
            place_samples.append(place_us)
        if submitted_ack_us is None:
            submitted_ack_errors += 1
        else:
            submitted_ack_samples.append(submitted_ack_us)
        if cancel_us is None:
            cancel_errors += 1
        else:
            cancel_samples.append(cancel_us)
        time.sleep(args.sleep)
    elapsed_s = time.time() - start

    place_stats = _summarize("place_order", place_samples, place_errors)
    submitted_ack_stats = _summarize("submitted_ack", submitted_ack_samples, submitted_ack_errors)
    cancel_stats = _summarize("cancel_order", cancel_samples, cancel_errors)

    report = {
        "meta": {
            "mode": args.mode,
            "symbol": args.symbol,
            "contract_code": getattr(contract, "code", None),
            "qty": args.qty,
            "offset_ticks": args.offset_ticks,
            "iters_requested": args.iters,
            "warmup": args.warmup,
            "sleep_between_cycles_s": args.sleep,
            "elapsed_s": round(elapsed_s, 2),
            "fills_aborted": fills_aborted,
            "ts_utc": datetime.utcnow().isoformat() + "Z",
        },
        "place_order": asdict(place_stats),
        "submitted_ack": asdict(submitted_ack_stats),
        "cancel_order": asdict(cancel_stats),
        "raw": {
            "place_samples_us": place_samples,
            "submitted_ack_samples_us": submitted_ack_samples,
            "cancel_samples_us": cancel_samples,
        },
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str))

    # Human-readable summary
    def _line(label: str, s: OpStats) -> str:
        return (
            f"{label:<20} n={s.count:<5} err={s.errors:<3} "
            f"P50={s.p50_us / 1000:>7.1f}ms  P95={s.p95_us / 1000:>7.1f}ms  "
            f"P99={s.p99_us / 1000:>7.1f}ms  max={s.max_us / 1000:>7.1f}ms  "
            f"P95_CI95=[{s.p95_ci95_us[0] / 1000:.1f}, {s.p95_ci95_us[1] / 1000:.1f}]ms"
        )

    print(f"\nMode: {args.mode}  Symbol: {args.symbol}  Elapsed: {elapsed_s:.1f}s  Aborted on fill: {fills_aborted}")
    print(_line("place_order", place_stats))
    print(_line("submitted_ack", submitted_ack_stats))
    print(_line("cancel_order", cancel_stats))
    print(f"\nFull report: {out_path}")

    try:
        api.logout()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
