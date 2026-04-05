"""R31-11: Deep analysis of TWSE volume concentration at open/close.
86% of 2330 volume is in first 30 min! This is huge for strategy design.
Also check: is the opening auction or continuous trading?
"""
import pandas as pd
import numpy as np
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")

# Check if opening volume is a single opening auction tick or continuous
sym = "2330"
dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))

print("=== OPENING VOLUME STRUCTURE (2330) ===\n")
for date_str in dates[:3]:
    df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
    ticks = df[df["type"] == "Tick"].sort_values("exch_ts").head(20)
    print(f"Date: {date_str}, first 20 ticks:")
    for _, row in ticks.iterrows():
        ts_s = row["exch_ts"] / 1e9
        tod_s = (ts_s + 8 * 3600) % 86400
        h = int(tod_s // 3600)
        m = int((tod_s % 3600) // 60)
        s = tod_s % 60
        print(f"  {h:02d}:{m:02d}:{s:06.3f}  price={row['price_scaled']}  vol={row['volume']}")
    print()

# Check total opening volume vs rest
print("\n=== OPENING AUCTION vs CONTINUOUS ===")
for sym in ["2330", "2317", "2303", "2881", "1303", "2409"]:
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    for date_str in dates[:2]:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        ticks = df[df["type"] == "Tick"].sort_values("exch_ts")
        ts_s = ticks["exch_ts"].values / 1e9
        tod_s = (ts_s + 8 * 3600) % 86400

        # First tick
        first_time = tod_s[0]
        first_vol = ticks.iloc[0]["volume"]
        total_vol = ticks["volume"].sum()

        # Volume in first 5 seconds vs first 5 minutes
        mask_5s = tod_s < (first_time + 5)
        mask_5m = tod_s < (first_time + 300)
        vol_5s = ticks[mask_5s]["volume"].sum()
        vol_5m = ticks[mask_5m]["volume"].sum()
        n_ticks_5s = mask_5s.sum()
        n_ticks_5m = mask_5m.sum()

        h = int(first_time // 3600)
        m = int((first_time % 3600) // 60)
        s = first_time % 60

        print(f"  {sym} {date_str}: open={h:02d}:{m:02d}:{s:04.1f}, "
              f"1st_tick_vol={first_vol} ({first_vol/total_vol*100:.1f}%), "
              f"5s_vol={vol_5s} ({vol_5s/total_vol*100:.1f}%, {n_ticks_5s} ticks), "
              f"5m_vol={vol_5m} ({vol_5m/total_vol*100:.1f}%, {n_ticks_5m} ticks)")


# Check closing auction
print("\n=== CLOSING AUCTION PATTERN ===")
for sym in ["2330", "2317", "1303", "2881"]:
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    for date_str in dates[:2]:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        ticks = df[df["type"] == "Tick"].sort_values("exch_ts")
        ts_s = ticks["exch_ts"].values / 1e9
        tod_s = (ts_s + 8 * 3600) % 86400

        # Last 5 minutes
        last_time = tod_s[-1]
        mask_last5m = tod_s > (last_time - 300)
        vol_last5m = ticks[mask_last5m]["volume"].sum()
        total_vol = ticks["volume"].sum()

        last_vol = ticks.iloc[-1]["volume"]

        h = int(last_time // 3600)
        m = int((last_time % 3600) // 60)

        print(f"  {sym} {date_str}: close={h:02d}:{m:02d}, "
              f"last_tick_vol={last_vol} ({last_vol/total_vol*100:.1f}%), "
              f"last5m_vol={vol_last5m} ({vol_last5m/total_vol*100:.1f}%)")
