"""R31-11b: Understand the actual data format.
Opening auction data is cumulative volume snapshots.
TWSE: 08:30-09:00 opening auction, 09:00-13:25 continuous, 13:25-13:30 closing auction.
"""
import pandas as pd
import numpy as np
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")

def ts_to_local(ts_ns):
    """Convert ns timestamp to local HH:MM:SS."""
    ts_s = ts_ns / 1e9
    tod_s = (ts_s + 8 * 3600) % 86400
    h = int(tod_s // 3600)
    m = int((tod_s % 3600) // 60)
    s = tod_s % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"

# Look at volume changes (diff) to find actual traded volume
sym = "2330"
date_str = "2026-02-06"
df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
ticks = df[df["type"] == "Tick"].sort_values("exch_ts").reset_index(drop=True)

print("=== 2330 2026-02-06: Volume pattern ===")
print("Check if volume is cumulative or per-tick:\n")

for i in range(min(30, len(ticks))):
    row = ticks.iloc[i]
    print(f"  {ts_to_local(row['exch_ts'])}  price={row['price_scaled']/1e7:.1f}  "
          f"vol={row['volume']}  diff={row['volume'] - (ticks.iloc[i-1]['volume'] if i > 0 else 0)}")

# Check around 09:00 (continuous trading start)
print("\n\nAround 09:00 (continuous trading start):")
ts_s = ticks["exch_ts"].values / 1e9
tod_s = (ts_s + 8 * 3600) % 86400
mask_9am = (tod_s >= 8.5 * 3600) & (tod_s <= 9.1 * 3600)  # 08:30 to 09:06
ticks_9 = ticks[mask_9am]
for i, (_, row) in enumerate(ticks_9.iterrows()):
    print(f"  {ts_to_local(row['exch_ts'])}  price={row['price_scaled']/1e7:.1f}  vol={row['volume']}")
    if i > 30:
        break

# Check if volume resets after auction
print("\n\nLate morning ticks (10:00-10:05):")
mask_10 = (tod_s >= 10 * 3600) & (tod_s <= 10.08 * 3600)
ticks_10 = ticks[mask_10].head(20)
for _, row in ticks_10.iterrows():
    print(f"  {ts_to_local(row['exch_ts'])}  price={row['price_scaled']/1e7:.1f}  vol={row['volume']}")

# Closing auction
print("\n\nClosing period (13:20-13:30):")
mask_close = (tod_s >= 13.33 * 3600) & (tod_s <= 13.5 * 3600)
ticks_close = ticks[mask_close].tail(20)
for _, row in ticks_close.iterrows():
    print(f"  {ts_to_local(row['exch_ts'])}  price={row['price_scaled']/1e7:.1f}  vol={row['volume']}")

# Check the distribution of volume values
print("\n\n=== Volume statistics ===")
vols = ticks["volume"].values
vol_diff = np.diff(vols)
print(f"  Min vol: {vols.min()}, Max vol: {vols.max()}")
print(f"  Median vol: {np.median(vols)}")
print(f"  Mean vol: {vols.mean():.1f}")
print(f"  Vol diff min: {vol_diff.min()}, max: {vol_diff.max()}")
print(f"  Vol diff mean: {vol_diff.mean():.1f}")
print(f"  Negative diffs (vol resets): {(vol_diff < 0).sum()}")
print(f"  Zero diffs: {(vol_diff == 0).sum()}")

# If volume is per-tick (not cumulative), small values expected for individual trades
# If cumulative, values grow throughout the day
print(f"\n  First 5 vols: {vols[:5]}")
print(f"  Middle vols: {vols[len(vols)//2:len(vols)//2+5]}")
print(f"  Last 5 vols: {vols[-5:]}")
