"""
Export aligned 2330 + TMF day-session data from ClickHouse.

For each date where both 2330 and a TMF contract have >5K BidAsk rows
in the day session (UTC hour 0-6), exports aligned npz files.

TMF front-month selection:
  - Jan 26 - Feb 23: TMFB6
  - Feb 24 - Mar 18: TMFC6 (fallback TMFD6)
  - Mar 19+: TMFD6

Output: research/data/processed/tsmc_leadlag/aligned_{date}.npz
  Contains 'stock' and 'futures' structured arrays with fields:
  mid_price (float64), bid_qty (float64), ask_qty (float64),
  volume (float64), local_ts (int64 nanoseconds)
"""

import subprocess
import numpy as np
from pathlib import Path
from datetime import date

BASE = Path(__file__).resolve().parent.parent.parent.parent.parent
OUT_DIR = BASE / "research" / "data" / "processed" / "tsmc_leadlag"
OUT_DIR.mkdir(parents=True, exist_ok=True)

PRICE_SCALE = 1e6  # bids_price / 1e6 = NTD for stocks, points for futures

# TMF contract schedule
TMF_SCHEDULE = [
    (date(2026, 1, 26), date(2026, 2, 23), "TMFB6"),
    (date(2026, 2, 24), date(2026, 3, 18), "TMFC6"),
    (date(2026, 3, 19), date(2026, 12, 31), "TMFD6"),
]

# Fallback contracts if primary has <5K rows
TMF_FALLBACKS = {
    "TMFB6": ["TMFD6"],
    "TMFC6": ["TMFD6"],
    "TMFD6": ["TMFC6"],
}


def get_tmf_contract(dt: date) -> list:
    """Return ordered list of TMF contracts to try for a given date."""
    for start, end, symbol in TMF_SCHEDULE:
        if start <= dt <= end:
            return [symbol] + TMF_FALLBACKS.get(symbol, [])
    return ["TMFD6"]


def ch_query(sql: str) -> str:
    """Run a ClickHouse query and return stdout."""
    result = subprocess.run(
        ["docker", "exec", "clickhouse", "clickhouse-client", "--query", sql],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ClickHouse error: {result.stderr.strip()}")
    return result.stdout.strip()


def export_symbol_day(symbol: str, date_str: str) -> np.ndarray | None:
    """Export BidAsk rows for a symbol on a given date (day session only).

    Returns structured array or None if insufficient data.
    """
    sql = f"""
    SELECT
        exch_ts,
        bids_price[1] as bid_px,
        asks_price[1] as ask_px,
        bids_vol[1] as bid_qty,
        asks_vol[1] as ask_qty,
        volume
    FROM hft.market_data
    WHERE symbol = '{symbol}'
      AND type = 'BidAsk'
      AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date_str}'
      AND toHour(toDateTime64(exch_ts/1e9, 3)) >= 0
      AND toHour(toDateTime64(exch_ts/1e9, 3)) < 6
      AND bids_price[1] > 0
      AND asks_price[1] > 0
    ORDER BY exch_ts
    FORMAT TabSeparated
    """
    raw = ch_query(sql)
    if not raw:
        return None

    lines = raw.split("\n")
    if len(lines) < 5000:
        return None

    dtype = np.dtype([
        ("mid_price", "f8"),
        ("bid_qty", "f8"),
        ("ask_qty", "f8"),
        ("volume", "f8"),
        ("local_ts", "i8"),
    ])
    arr = np.empty(len(lines), dtype=dtype)

    for i, line in enumerate(lines):
        parts = line.split("\t")
        exch_ts = int(parts[0])
        bid_px = int(parts[1]) / PRICE_SCALE
        ask_px = int(parts[2]) / PRICE_SCALE
        bid_qty = float(parts[3])
        ask_qty = float(parts[4])
        vol = float(parts[5])

        arr[i]["mid_price"] = (bid_px + ask_px) / 2.0
        arr[i]["bid_qty"] = bid_qty
        arr[i]["ask_qty"] = ask_qty
        arr[i]["volume"] = vol
        arr[i]["local_ts"] = exch_ts

    return arr


def discover_dates() -> list:
    """Find all dates with 2330 BidAsk data in day session."""
    sql = """
    SELECT toDate(toDateTime64(exch_ts/1e9, 3)) as dt, count() as cnt
    FROM hft.market_data
    WHERE symbol = '2330'
      AND type = 'BidAsk'
      AND toHour(toDateTime64(exch_ts/1e9, 3)) >= 0
      AND toHour(toDateTime64(exch_ts/1e9, 3)) < 6
      AND bids_price[1] > 0
    GROUP BY dt
    HAVING cnt > 5000
    ORDER BY dt
    FORMAT TabSeparated
    """
    raw = ch_query(sql)
    dates = []
    for line in raw.split("\n"):
        if line.strip():
            parts = line.split("\t")
            dates.append(parts[0])
    return dates


def run():
    print("Discovering dates with 2330 data...")
    dates = discover_dates()
    print(f"Found {len(dates)} dates with 2330 BidAsk > 5K rows\n")

    exported = 0
    skipped = 0

    for date_str in dates:
        dt = date.fromisoformat(date_str)
        contracts = get_tmf_contract(dt)

        print(f"{date_str}: trying {contracts}...", end=" ", flush=True)

        # Export 2330
        stock = export_symbol_day("2330", date_str)
        if stock is None:
            print("SKIP (2330 < 5K)")
            skipped += 1
            continue

        # Try TMF contracts in order
        futures = None
        used_contract = None
        for contract in contracts:
            futures = export_symbol_day(contract, date_str)
            if futures is not None:
                used_contract = contract
                break

        if futures is None:
            print(f"SKIP (no TMF > 5K)")
            skipped += 1
            continue

        # Save
        out_path = OUT_DIR / f"aligned_{date_str}.npz"
        np.savez_compressed(out_path, stock=stock, futures=futures)
        print(f"OK — 2330={len(stock)}, {used_contract}={len(futures)}")
        exported += 1

    print(f"\nDone: {exported} exported, {skipped} skipped")


if __name__ == "__main__":
    run()
