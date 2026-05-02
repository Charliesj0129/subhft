#!/usr/bin/env python3
"""Backfill missing tick data from Shioaji historical API into ClickHouse.

Usage (inside hft-engine container or host with shioaji installed):
    python scripts/backfill_historical_ticks.py --dates 2026-04-06,2026-04-07 [--time-end 10:45:00]

Rate limit: 50 requests per 5 seconds. Script auto-throttles.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

PRICE_SCALE = 1_000_000  # CK uses x1e6
TZ_OFFSET_NS = 8 * 3600 * 1_000_000_000  # Shioaji historical ts is local (TST), CK needs UTC epoch
CH_HOST = os.getenv("HFT_CLICKHOUSE_HOST", "clickhouse")
CH_PORT = os.getenv("HFT_CLICKHOUSE_PORT", "8123")
CH_USER = os.getenv("HFT_CLICKHOUSE_USER", "default")
CH_PASSWORD = os.getenv("HFT_CLICKHOUSE_PASSWORD", os.getenv("CLICKHOUSE_PASSWORD", ""))
CH_TABLE = "hft.market_data"

# Symbol → (exchange, instrument_type, category)
FUTURES = {
    "TXFD6": ("FUT", "future", "TXF"),
    "TMFD6": ("FUT", "future", "TMF"),
    "MXFD6": ("FUT", "future", "MXF"),
    "TXFF6": ("FUT", "future", "TXF"),
    "TMFF6": ("FUT", "future", "TMF"),
    "MXFF6": ("FUT", "future", "MXF"),
}

STOCKS = [
    "1101", "1102", "1216", "1301", "1303", "1326", "1402",
    "2002", "2201", "2207", "2301", "2303", "2308", "2317",
    "2327", "2330", "2345", "2354", "2357", "2379", "2382",
    "2395", "2408", "2409", "2412", "2454", "2474", "2603",
    "2609", "2615", "2801", "2881", "2882", "2883", "2884",
    "2885", "2886", "2887", "2890", "2891", "2892", "2912",
    "3008", "3034", "3045", "3711", "4904", "4938", "5871", "5880",
]

# Rate limiter: 50 requests per 5 seconds
_req_times: list[float] = []
RATE_LIMIT = 50
RATE_WINDOW = 5.0


def _throttle() -> None:
    """Block until we can make another request within rate limits."""
    now = time.monotonic()
    _req_times[:] = [t for t in _req_times if now - t < RATE_WINDOW]
    if len(_req_times) >= RATE_LIMIT:
        wait = RATE_WINDOW - (now - _req_times[0]) + 0.1
        if wait > 0:
            print(f"  [throttle] waiting {wait:.1f}s for rate limit...")
            time.sleep(wait)
    _req_times.append(time.monotonic())


def ch_insert(rows: list[dict]) -> int:
    """Insert rows into ClickHouse via HTTP API. Returns row count."""
    if not rows:
        return 0
    ndjson = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    auth_params = ""
    if CH_USER:
        auth_params += f"&user={CH_USER}"
    if CH_PASSWORD:
        auth_params += f"&password={CH_PASSWORD}"
    url = f"http://{CH_HOST}:{CH_PORT}/?query=INSERT+INTO+{CH_TABLE}+FORMAT+JSONEachRow{auth_params}"
    req = urllib.request.Request(url, data=ndjson.encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return len(rows)
    except Exception as exc:
        print(f"  [ERROR] CK insert failed: {exc}")
        return 0


def tick_type_to_direction(tt: int) -> int:
    """Map Shioaji tick_type (1=外盤buy, 2=內盤sell) to trade_direction."""
    if tt == 1:
        return 1
    if tt == 2:
        return -1
    return 0


def fetch_and_insert(
    api,
    contract,
    symbol: str,
    exchange: str,
    instrument_type: str,
    date: str,
    time_start: str | None = None,
    time_end: str | None = None,
) -> int:
    """Fetch historical ticks for one contract/date and insert into CK."""
    import shioaji as sj

    _throttle()

    kwargs: dict = {"contract": contract, "date": date}
    if time_start and time_end:
        kwargs["query_type"] = sj.constant.TicksQueryType.RangeTime
        kwargs["time_start"] = time_start
        kwargs["time_end"] = time_end

    try:
        ticks = api.ticks(**kwargs)
    except Exception as exc:
        print(f"  [WARN] {symbol} {date}: ticks API error: {exc}")
        return 0

    if not ticks or not ticks.ts:
        print(f"  [SKIP] {symbol} {date}: no data")
        return 0

    n = len(ticks.ts)
    rows: list[dict] = []
    batch_size = 5000

    for i in range(n):
        ts_ns = ticks.ts[i] - TZ_OFFSET_NS  # convert local epoch → UTC epoch
        price = ticks.close[i]
        vol = ticks.volume[i]
        tt = ticks.tick_type[i] if hasattr(ticks, "tick_type") and i < len(ticks.tick_type) else 0

        row = {
            "symbol": symbol,
            "exchange": exchange,
            "type": "Tick",
            "exch_ts": ts_ns,
            "ingest_ts": ts_ns,  # use exch_ts as proxy for backfill
            "price_scaled": int(price * PRICE_SCALE),
            "volume": vol,
            "bids_price": [],
            "bids_vol": [],
            "asks_price": [],
            "asks_vol": [],
            "seq_no": i,
            "trade_direction": tick_type_to_direction(tt),
            "instrument_type": instrument_type,
            "underlying": "",
            "strike_scaled": 0,
            "option_right": "",
            "expiry": "1970-01-01",
        }
        rows.append(row)

        if len(rows) >= batch_size:
            ch_insert(rows)
            rows.clear()

    if rows:
        ch_insert(rows)

    return n


def resolve_contract(api, symbol: str):
    """Resolve a symbol string to a Shioaji contract object."""
    if symbol in FUTURES:
        _, _, category = FUTURES[symbol]
        try:
            cat_obj = getattr(api.Contracts.Futures, category, None)
            if cat_obj is None:
                return None
            return cat_obj[symbol]
        except (KeyError, AttributeError, IndexError):
            return None
    # Stock
    try:
        return api.Contracts.Stocks[symbol]
    except (KeyError, AttributeError):
        return None


def main():
    parser = argparse.ArgumentParser(description="Backfill historical ticks to ClickHouse")
    parser.add_argument("--dates", required=True, help="Comma-separated dates (YYYY-MM-DD)")
    parser.add_argument("--time-start", default=None, help="Start time filter (HH:MM:SS), applies to ALL dates")
    parser.add_argument("--time-end", default=None, help="End time filter (HH:MM:SS), applies to ALL dates")
    parser.add_argument("--symbols", default=None, help="Comma-separated symbols (default: all futures+stocks)")
    parser.add_argument("--futures-only", action="store_true", help="Only backfill futures")
    parser.add_argument("--dry-run", action="store_true", help="Login and resolve contracts but don't insert")
    args = parser.parse_args()

    import shioaji as sj

    dates = [d.strip() for d in args.dates.split(",")]

    print(f"=== Backfill Historical Ticks ===")
    print(f"Dates: {dates}")
    if args.time_start or args.time_end:
        print(f"Time filter: {args.time_start} - {args.time_end}")

    # Login
    api = sj.Shioaji()
    api_key = os.getenv("SHIOAJI_API_KEY")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY")
    if not api_key or not secret_key:
        print("[ERROR] SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY must be set")
        sys.exit(1)

    print("Logging in to Shioaji...")
    try:
        api.login(api_key=api_key, secret_key=secret_key)
    except Exception as exc:
        print(f"[ERROR] Login failed: {exc}")
        sys.exit(1)
    print("Login OK")

    # Build symbol list
    if args.symbols:
        target_symbols = [s.strip() for s in args.symbols.split(",")]
    elif args.futures_only:
        target_symbols = list(FUTURES.keys())
    else:
        target_symbols = list(FUTURES.keys()) + STOCKS

    print(f"Symbols: {len(target_symbols)}")

    total_ticks = 0
    total_errors = 0

    try:
        for date in dates:
            print(f"\n--- Date: {date} ---")
            for sym in target_symbols:
                contract = resolve_contract(api, sym)
                if contract is None:
                    print(f"  [SKIP] {sym}: contract not found")
                    total_errors += 1
                    continue

                if sym in FUTURES:
                    exchange, inst_type, _ = FUTURES[sym]
                else:
                    exchange = "TSE"
                    inst_type = "stock"

                if args.dry_run:
                    print(f"  [DRY] {sym} ({exchange}): would fetch")
                    continue

                n = fetch_and_insert(
                    api, contract, sym, exchange, inst_type, date,
                    time_start=args.time_start,
                    time_end=args.time_end,
                )
                if n > 0:
                    print(f"  [OK] {sym}: {n:,} ticks inserted")
                    total_ticks += n
    finally:
        print(f"\n=== Done: {total_ticks:,} ticks inserted, {total_errors} errors ===")
        try:
            api.logout()
            print("Logged out")
        except Exception:
            pass


if __name__ == "__main__":
    main()
