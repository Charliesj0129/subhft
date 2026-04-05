"""R31 Stage 1: Data reconnaissance — schema, sizes, date overlap."""
import pandas as pd
import os
from pathlib import Path

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")

# 1. Schema check on one file
sample = pd.read_parquet(DATA / "2330" / "2026-03-23.parquet")
print("=== 2330 schema ===")
print(sample.dtypes)
print(f"Rows: {len(sample)}")
print(sample.head(3).to_string())
print()

# Check type column values
print("Types:", sample["type"].unique() if "type" in sample.columns else "NO TYPE COL")

# 2. Check futures schema
fut = pd.read_parquet(DATA / "TXFD6" / "2026-03-23.parquet")
print("\n=== TXFD6 schema ===")
print(fut.dtypes)
print(f"Rows: {len(fut)}")
print(fut.head(3).to_string())

# 3. Date overlap across key symbols
symbols = ["2330", "2317", "2303", "2454", "2881", "2882", "2884", "2886", "2891", "2892",
           "TXFD6", "TXFC6", "MXFD6", "TMFD6"]
print("\n=== Date coverage ===")
date_map = {}
for sym in symbols:
    d = DATA / sym
    if d.exists():
        dates = sorted([f.stem for f in d.glob("*.parquet")])
        date_map[sym] = dates
        print(f"{sym:10s}: {len(dates)} days  {dates[0]}..{dates[-1]}")
    else:
        print(f"{sym:10s}: MISSING")

# 4. Find common dates across stocks + TXFD6
stock_syms = ["2330", "2317", "2303", "2454"]
all_date_sets = [set(date_map.get(s, [])) for s in stock_syms + ["TXFD6"]]
common = sorted(set.intersection(*all_date_sets)) if all_date_sets else []
print(f"\nCommon dates (top4 stocks + TXFD6): {len(common)} days")
for d in common:
    print(f"  {d}")

# 5. Options check
opt_dirs = sorted([d.name for d in DATA.iterdir() if d.name.startswith("TXO")])
print(f"\nTXO symbols: {len(opt_dirs)}")
if opt_dirs:
    # Check one option
    opt_sample_dir = DATA / opt_dirs[0]
    opt_files = sorted(opt_sample_dir.glob("*.parquet"))
    if opt_files:
        opt_df = pd.read_parquet(opt_files[0])
        print(f"Sample option ({opt_dirs[0]}, {opt_files[0].stem}): {len(opt_df)} rows")
        print(opt_df.head(2).to_string())
