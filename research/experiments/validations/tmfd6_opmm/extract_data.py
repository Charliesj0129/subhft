"""
Extract TMFD6 BidAsk data from ClickHouse for OpportunisticMM backtest.
Exports to numpy arrays per day to avoid memory issues.
"""
import subprocess
import csv
import io
import numpy as np
import os
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent / "data"
OUTPUT_DIR.mkdir(exist_ok=True)

# Get list of days
result = subprocess.run(
    [
        "docker", "exec", "clickhouse", "clickhouse-client",
        "--query",
        """
        SELECT DISTINCT toDate(toDateTime(exch_ts/1000000000)) AS day
        FROM hft.market_data
        WHERE symbol='TMFD6' AND type='BidAsk'
        ORDER BY day
        SETTINGS max_memory_usage=3000000000
        """,
    ],
    capture_output=True, text=True,
)
days = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
print(f"Found {len(days)} days: {days}")

for day in days:
    outfile = OUTPUT_DIR / f"tmfd6_{day}.npz"
    if outfile.exists():
        print(f"  {day}: already exists, skipping")
        continue

    print(f"  Extracting {day}...")
    query = f"""
    SELECT
        exch_ts,
        arrayElement(bids_price, 1) AS bid1_price,
        arrayElement(bids_vol, 1) AS bid1_vol,
        arrayElement(asks_price, 1) AS ask1_price,
        arrayElement(asks_vol, 1) AS ask1_vol,
        if(length(bids_price) >= 5, arrayElement(bids_price, 5), 0) AS bid5_price,
        if(length(bids_vol) >= 5, arrayElement(bids_vol, 5), 0) AS bid5_vol,
        if(length(asks_price) >= 5, arrayElement(asks_price, 5), 0) AS ask5_price,
        if(length(asks_vol) >= 5, arrayElement(asks_vol, 5), 0) AS ask5_vol
    FROM hft.market_data
    WHERE symbol='TMFD6' AND type='BidAsk'
        AND length(bids_price) > 0 AND length(asks_price) > 0
        AND arrayElement(asks_price, 1) > arrayElement(bids_price, 1)
        AND toDate(toDateTime(exch_ts/1000000000)) = '{day}'
    ORDER BY exch_ts
    FORMAT CSVWithNames
    SETTINGS max_memory_usage=3000000000
    """
    result = subprocess.run(
        ["docker", "exec", "clickhouse", "clickhouse-client", "--query", query],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    ERROR: {result.stderr[:200]}")
        continue

    reader = csv.DictReader(io.StringIO(result.stdout))
    rows = list(reader)
    if not rows:
        print(f"    No data")
        continue

    n = len(rows)
    exch_ts = np.zeros(n, dtype=np.int64)
    bid1_price = np.zeros(n, dtype=np.int64)
    bid1_vol = np.zeros(n, dtype=np.int64)
    ask1_price = np.zeros(n, dtype=np.int64)
    ask1_vol = np.zeros(n, dtype=np.int64)
    bid5_price = np.zeros(n, dtype=np.int64)
    bid5_vol = np.zeros(n, dtype=np.int64)
    ask5_price = np.zeros(n, dtype=np.int64)
    ask5_vol = np.zeros(n, dtype=np.int64)

    for i, row in enumerate(rows):
        exch_ts[i] = int(row["exch_ts"])
        bid1_price[i] = int(row["bid1_price"])
        bid1_vol[i] = int(row["bid1_vol"])
        ask1_price[i] = int(row["ask1_price"])
        ask1_vol[i] = int(row["ask1_vol"])
        bid5_price[i] = int(row["bid5_price"])
        bid5_vol[i] = int(row["bid5_vol"])
        ask5_price[i] = int(row["ask5_price"])
        ask5_vol[i] = int(row["ask5_vol"])

    np.savez_compressed(
        outfile,
        exch_ts=exch_ts,
        bid1_price=bid1_price, bid1_vol=bid1_vol,
        ask1_price=ask1_price, ask1_vol=ask1_vol,
        bid5_price=bid5_price, bid5_vol=bid5_vol,
        ask5_price=ask5_price, ask5_vol=ask5_vol,
    )
    print(f"    Saved {n} rows to {outfile}")

print("Done.")
