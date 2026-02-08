import glob
import json
import os
from typing import Optional

import numpy as np
from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("wal.converter")


class WALConverter:
    """
    Converts JSONL WAL logs (Market Data) into deterministic NPZ format
    for high-fidelity replay simulation.
    """

    def __init__(self, wal_dir: str, output_dir: str):
        self.wal_dir = wal_dir
        self.output_dir = output_dir

    def convert(self, date_str: str, symbol: Optional[str] = None):
        """
        Convert logs for a specific date and optional symbol.
        Output: {output_dir}/{symbol}_{date}.npz
        """
        logger.info("Starting WAL conversion", date=date_str, symbol=symbol)

        # 1. Find all relevant files
        # Pattern: market_data_YYYYMMDD*.jsonl
        # or just scan all log files and filter by content/date
        # Assuming filename contains timestamp or we grep?
        # For prototype, we scan all market_data*.jsonl and filter inside
        files = glob.glob(os.path.join(self.wal_dir, "market_data*.jsonl"))

        raw_rows = []

        for fpath in files:
            with open(fpath, "r") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                        # Filter by Date/Symbol
                        # Assuming row has 'exch_ts' (ns)
                        # Filter date logic (simple check)
                        # TODO: Robust date parsing
                        if symbol and row.get("symbol") != symbol:
                            continue

                        raw_rows.append(row)
                    except json.JSONDecodeError as e:
                        logger.warning(
                            "Skipping corrupt JSON line",
                            file=fpath,
                            error=str(e),
                        )
                        continue

        if not raw_rows:
            logger.warning("No data found", date=date_str, symbol=symbol)
            return

        # 2. Sort Deterministically
        # Primary: exch_ts, Secondary: seq_no (if avail), Tertiary: ingest_ts
        raw_rows.sort(key=lambda x: (x.get("exch_ts", 0), x.get("seq_no", 0), x.get("ingest_ts", 0)))

        # 3. Create Numpy Structured Array
        # HftBacktest format: [event_flags, exch_ts, local_ts, price, qty] ...
        # But for L2 replay, we likely need specialized format OR Mapping
        # We will use a custom format that our BacktestAdapter can read.
        # [timestamp, type, price, qty, bids[5], asks[5]] is heavy.
        # We stick to hftbacktest standard dense array if possible, or custom.
        # Custom "Event" array:
        # dtype: [('ev', 'u8'), ('exch_ts', 'u8'), ('local_ts', 'u8'),
        #         ('price', 'f8'), ('qty', 'f8'),
        #         ('bid_p_0', 'f8'), ('bid_v_0', 'f8'), ... ]

        dtype_fields = [
            ("ev", "u8"),
            ("exch_ts", "u8"),
            ("local_ts", "u8"),
            ("price", "f8"),
            ("qty", "f8"),
        ]
        # Add L2 Depth (5 levels)
        for i in range(5):
            dtype_fields.append((f"bid_p_{i}", "f8"))
            dtype_fields.append((f"bid_v_{i}", "f8"))
            dtype_fields.append((f"ask_p_{i}", "f8"))
            dtype_fields.append((f"ask_v_{i}", "f8"))

        count = len(raw_rows)
        data = np.zeros(count, dtype=dtype_fields)

        # 4. Fill Data
        for i, row in enumerate(raw_rows):
            # Map Type string to Int Event
            # 'trade' -> 1, 'book' -> 2
            ev_type = 1 if row.get("type") == "trade" else 2

            data[i]["ev"] = ev_type
            data[i]["exch_ts"] = int(row.get("exch_ts", 0))
            data[i]["local_ts"] = int(row.get("ingest_ts", 0))  # Using ingest as local arrival
            data[i]["price"] = float(row.get("price", 0))
            data[i]["qty"] = float(row.get("volume", 0))

            # L2
            bids_p = row.get("bids_price", [])
            bids_v = row.get("bids_vol", [])
            asks_p = row.get("asks_price", [])
            asks_v = row.get("asks_vol", [])

            for lvl in range(5):
                if lvl < len(bids_p):
                    data[i][f"bid_p_{lvl}"] = float(bids_p[lvl])
                    data[i][f"bid_v_{lvl}"] = float(bids_v[lvl])
                if lvl < len(asks_p):
                    data[i][f"ask_p_{lvl}"] = float(asks_p[lvl])
                    data[i][f"ask_v_{lvl}"] = float(asks_v[lvl])

        # 5. Save
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

        out_name = f"{symbol}_{date_str}.npz" if symbol else f"full_{date_str}.npz"
        out_path = os.path.join(self.output_dir, out_name)

        # Include Metadata
        metadata = {
            "created_at": timebase.now_s(),
            "source_files": len(files),
            "rows": count,
            "git_commit": os.getenv("GIT_COMMIT", "unknown"),
            "config_hash": "TODO",  # Calculate hash of current config/symbols
            "seed": 42,  # Default seed for this dataset provenance
        }

        np.savez_compressed(out_path, data=data, metadata=json.dumps(metadata))
        logger.info("Conversion complete", path=out_path, rows=count)


if __name__ == "__main__":
    import sys

    # Usage: python -m hft_platform.recorder.converter <source_dir> <out_dir> <date> <symbol>
    if len(sys.argv) > 2:
        wal_d = sys.argv[1]
        out_d = sys.argv[2]
        date_s = sys.argv[3] if len(sys.argv) > 3 else "today"
        sym_s = sys.argv[4] if len(sys.argv) > 4 else "2330"

        c = WALConverter(wal_d, out_d)
        c.convert(date_s, sym_s)
