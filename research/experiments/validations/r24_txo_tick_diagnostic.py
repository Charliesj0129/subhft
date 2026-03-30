"""R24 Diagnostic 0c: TXO trade tick density measurement.

Subscribes to ATM +/- 5 TXO strikes and counts Tick vs BidAsk events
over a configurable observation window. Requires live Shioaji session.

Usage:
    # During market hours (08:45-13:45 Taiwan time):
    SHIOAJI_API_KEY=... SHIOAJI_SECRET_KEY=... \
        python research/experiments/validations/r24_txo_tick_diagnostic.py

    # With custom observation window and ATM estimate:
    TXO_OBSERVE_MINUTES=60 TXO_ATM_ESTIMATE=23000 \
        python research/experiments/validations/r24_txo_tick_diagnostic.py

    # Save results to JSON for later analysis:
    TXO_OUTPUT_JSON=results.json \
        python research/experiments/validations/r24_txo_tick_diagnostic.py

Output: prints per-symbol and aggregate tick/bidask counts, then exits.
Optionally saves structured results to JSON via TXO_OUTPUT_JSON env var.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime

import structlog

logger = structlog.get_logger("r24_txo_diagnostic")

# How long to observe (minutes)
OBSERVE_MINUTES = int(os.getenv("TXO_OBSERVE_MINUTES", "30"))

# Strike range: ATM +/- N strikes (1 strike = 100 index points for TXO)
STRIKE_RANGE = int(os.getenv("TXO_STRIKE_RANGE", "5"))
STRIKE_STEP = 100  # TXO strike interval for near-ATM

# Counters
tick_counts: dict[str, int] = defaultdict(int)
bidask_counts: dict[str, int] = defaultdict(int)
tick_volumes: dict[str, int] = defaultdict(int)
first_tick_ts: dict[str, float] = {}
last_tick_ts: dict[str, float] = {}
_lock = threading.Lock()
_stop_event = threading.Event()


def _on_tick(_exchange: str, tick: object) -> None:
    """Shioaji FOP tick callback -- counts trade ticks."""
    code = getattr(tick, "code", None)
    if not code or not str(code).startswith("TXO"):
        return
    vol = int(getattr(tick, "volume", 0) or 0)
    ts = time.time()
    with _lock:
        tick_counts[code] += 1
        tick_volumes[code] += vol
        if code not in first_tick_ts:
            first_tick_ts[code] = ts
        last_tick_ts[code] = ts


def _on_bidask(_exchange: str, bidask: object) -> None:
    """Shioaji FOP bidask callback -- counts quote updates."""
    code = getattr(bidask, "code", None)
    if not code or not str(code).startswith("TXO"):
        return
    with _lock:
        bidask_counts[code] += 1


def _estimate_atm_strike() -> int:
    """Estimate current ATM strike from TAIEX level.

    In production, would read from live TAIEX quote.
    Override with TXO_ATM_ESTIMATE env var.
    """
    taiex_approx = int(os.getenv("TXO_ATM_ESTIMATE", "23000"))
    return (taiex_approx // STRIKE_STEP) * STRIKE_STEP


def _generate_txo_symbols(atm_strike: int) -> list[dict[str, str]]:
    """Generate TXO symbol codes for ATM +/- STRIKE_RANGE."""
    symbols: list[dict[str, str]] = []
    now = datetime.now()
    month_codes_call = {
        1: "A", 2: "B", 3: "C", 4: "D", 5: "E", 6: "F",
        7: "G", 8: "H", 9: "I", 10: "J", 11: "K", 12: "L",
    }
    month_codes_put = {
        1: "M", 2: "N", 3: "O", 4: "P", 5: "Q", 6: "R",
        7: "S", 8: "T", 9: "U", 10: "V", 11: "W", 12: "X",
    }
    month = now.month
    year_digit = now.year % 10
    call_suffix = f"{month_codes_call[month]}{year_digit}"
    put_suffix = f"{month_codes_put[month]}{year_digit}"

    for offset in range(-STRIKE_RANGE, STRIKE_RANGE + 1):
        strike = atm_strike + offset * STRIKE_STEP
        symbols.append({
            "code": f"TXO{strike}{call_suffix}",
            "exchange": "OPT",
            "product_type": "option",
        })
        symbols.append({
            "code": f"TXO{strike}{put_suffix}",
            "exchange": "OPT",
            "product_type": "option",
        })
    return symbols


def _print_results() -> None:
    """Print diagnostic summary."""
    with _lock:
        total_ticks = sum(tick_counts.values())
        total_bidask = sum(bidask_counts.values())
        total_volume = sum(tick_volumes.values())
        total_events = total_ticks + total_bidask

    pct_ticks = (total_ticks / total_events * 100) if total_events > 0 else 0

    print("\n" + "=" * 80)
    print(f"R24 DIAGNOSTIC 0c: TXO Trade Tick Density ({OBSERVE_MINUTES}min observation)")
    print("=" * 80)
    print(f"\nAggregate: {total_ticks:,} trade ticks / {total_bidask:,} quotes "
          f"({pct_ticks:.1f}% trades)")
    print(f"Total trade volume: {total_volume:,} contracts")

    if total_ticks > 0:
        extrapolated_daily = total_ticks * (270 / OBSERVE_MINUTES)  # 4.5h session
        print(f"Extrapolated daily trade ticks: ~{extrapolated_daily:,.0f}")
        print(f"Kill gate (>100/day): {'PASS' if extrapolated_daily > 100 else 'FAIL'}")
    else:
        print("No trade ticks received.")
        print("Check: (1) market hours? (2) correct ATM estimate? "
              "(3) FOP tick callback registered?")

    print(f"\nPer-symbol breakdown (top 20 by trade ticks):")
    print(f"{'Symbol':<20} {'Ticks':>8} {'Volume':>10} {'Quotes':>10} {'Tick%':>7}")
    print("-" * 60)

    with _lock:
        sorted_syms = sorted(tick_counts.keys(),
                             key=lambda s: tick_counts[s], reverse=True)

    for sym in sorted_syms[:20]:
        with _lock:
            t = tick_counts[sym]
            v = tick_volumes[sym]
            q = bidask_counts[sym]
        total = t + q
        pct = (t / total * 100) if total > 0 else 0
        print(f"{sym:<20} {t:>8,} {v:>10,} {q:>10,} {pct:>6.1f}%")

    # Symbols with quotes but ZERO trade ticks
    with _lock:
        quote_only = [
            s for s in bidask_counts
            if s not in tick_counts or tick_counts[s] == 0
        ]
    if quote_only:
        print(f"\nSymbols with quotes but NO trade ticks: {len(quote_only)}")
        for sym in quote_only[:10]:
            with _lock:
                q = bidask_counts[sym]
            print(f"  {sym}: {q:,} quotes, 0 ticks")

    print("\n" + "=" * 80)

    # Optionally save to JSON
    json_path = os.getenv("TXO_OUTPUT_JSON")
    if json_path:
        with _lock:
            result = {
                "date": datetime.now().isoformat(),
                "observe_minutes": OBSERVE_MINUTES,
                "strike_range": STRIKE_RANGE,
                "atm_estimate": int(os.getenv("TXO_ATM_ESTIMATE", "23000")),
                "total_trade_ticks": total_ticks,
                "total_quote_events": total_bidask,
                "total_trade_volume": total_volume,
                "trade_pct": round(pct_ticks, 2),
                "extrapolated_daily_ticks": (
                    round(total_ticks * (270 / OBSERVE_MINUTES))
                    if total_ticks > 0 else 0
                ),
                "per_symbol": {
                    sym: {
                        "ticks": tick_counts[sym],
                        "volume": tick_volumes[sym],
                        "quotes": bidask_counts.get(sym, 0),
                    }
                    for sym in sorted(
                        set(tick_counts) | set(bidask_counts),
                        key=lambda s: tick_counts.get(s, 0),
                        reverse=True,
                    )
                },
            }
        with open(json_path, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nResults saved to {json_path}")


def main() -> None:
    try:
        import shioaji as sj
    except ImportError:
        print("ERROR: shioaji not installed. Run: pip install 'shioaji[speed]'")
        sys.exit(1)

    api_key = os.environ.get("SHIOAJI_API_KEY")
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY")
    if not api_key or not secret_key:
        print("ERROR: Set SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY env vars")
        sys.exit(1)

    api = sj.Shioaji()
    logger.info("logging_in")
    api.login(api_key=api_key, secret_key=secret_key)
    logger.info("login_success")

    # Register FOP (futures/options) callbacks
    api.quote.set_on_tick_fop_v1_callback(_on_tick)
    api.quote.set_on_bidask_fop_v1_callback(_on_bidask)

    # Generate TXO symbols
    atm = _estimate_atm_strike()
    txo_symbols = _generate_txo_symbols(atm)
    logger.info("txo_diagnostic_config",
                atm_strike=atm, num_symbols=len(txo_symbols),
                observe_minutes=OBSERVE_MINUTES)

    # Verify Options contracts are available
    try:
        opts = api.Contracts.Options
        opt_codes = []
        for item in opts:
            for sub in item:
                c = getattr(sub, "code", None)
                if c and str(c).startswith("TXO"):
                    opt_codes.append(str(c))
        logger.info("available_txo_contracts", count=len(opt_codes),
                     sample=opt_codes[:10])
    except Exception as exc:
        logger.warning("failed_listing_options", error=str(exc))

    # Subscribe
    subscribed = 0
    failed: list[str] = []
    for sym in txo_symbols:
        code = sym["code"]
        try:
            contract = api.Contracts.Options[code]
            if contract is None:
                logger.warning("contract_not_found", code=code)
                failed.append(code)
                continue
            api.quote.subscribe(
                contract,
                quote_type=sj.constant.QuoteType.Tick,
                version=sj.constant.QuoteVersion.v1,
            )
            api.quote.subscribe(
                contract,
                quote_type=sj.constant.QuoteType.BidAsk,
                version=sj.constant.QuoteVersion.v1,
            )
            subscribed += 1
        except Exception as exc:
            logger.warning("subscribe_failed", code=code, error=str(exc))
            failed.append(code)

    logger.info("subscription_complete",
                subscribed=subscribed, failed=len(failed))

    if failed:
        logger.info("failed_symbols", codes=failed[:20])

    if subscribed == 0:
        print("ERROR: No TXO contracts subscribed successfully.")
        print("Possible causes:")
        print("  - Outside market hours (day: 08:45-13:45, night: 15:00-05:00)")
        print("  - Incorrect ATM estimate (set TXO_ATM_ESTIMATE env var)")
        print("  - TXO contract codes not matching Shioaji format")
        api.logout()
        sys.exit(1)

    # Handle Ctrl+C gracefully
    def _signal_handler(_sig: int, _frame: object) -> None:
        logger.info("interrupted")
        _stop_event.set()

    signal.signal(signal.SIGINT, _signal_handler)

    logger.info("observing",
                minutes=OBSERVE_MINUTES,
                interrupt="Ctrl+C to stop early")
    _stop_event.wait(timeout=OBSERVE_MINUTES * 60)

    # Print results
    _print_results()

    # Unsubscribe
    for sym in txo_symbols:
        code = sym["code"]
        try:
            contract = api.Contracts.Options[code]
            if contract is not None:
                api.quote.unsubscribe(
                    contract, quote_type=sj.constant.QuoteType.Tick)
                api.quote.unsubscribe(
                    contract, quote_type=sj.constant.QuoteType.BidAsk)
        except Exception:
            pass

    logger.info("logging_out")
    api.logout()
    logger.info("done")


if __name__ == "__main__":
    main()
