"""Quick price scale verification."""
import pandas as pd
from pathlib import Path

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")

# 2330 (TSMC) trades around ~900-1000 NTD in early 2026
df = pd.read_parquet(DATA / "2330" / "2026-03-23.parquet")
ticks = df[df["type"] == "Tick"]
print("2330 price_scaled range:")
print(f"  min={ticks['price_scaled'].min()}, max={ticks['price_scaled'].max()}")
print(f"  If /10000: {ticks['price_scaled'].min()/10000:.1f} .. {ticks['price_scaled'].max()/10000:.1f}")
print(f"  If /1e7:   {ticks['price_scaled'].min()/1e7:.1f} .. {ticks['price_scaled'].max()/1e7:.1f}")
print(f"  If /1e9:   {ticks['price_scaled'].min()/1e9:.1f} .. {ticks['price_scaled'].max()/1e9:.1f}")

# TXFD6 trades around ~20000-22000 points
df2 = pd.read_parquet(DATA / "TXFD6" / "2026-03-23.parquet")
ticks2 = df2[df2["type"] == "Tick"]
print("\nTXFD6 price_scaled range:")
print(f"  min={ticks2['price_scaled'].min()}, max={ticks2['price_scaled'].max()}")
print(f"  If /10000:   {ticks2['price_scaled'].min()/10000:.1f} .. {ticks2['price_scaled'].max()/10000:.1f}")
print(f"  If /1e6:     {ticks2['price_scaled'].min()/1e6:.1f} .. {ticks2['price_scaled'].max()/1e6:.1f}")
print(f"  If /1e9:     {ticks2['price_scaled'].min()/1e9:.1f} .. {ticks2['price_scaled'].max()/1e9:.1f}")

# 1301 trades around 80-100
df3 = pd.read_parquet(DATA / "1301" / "2026-03-23.parquet")
ticks3 = df3[df3["type"] == "Tick"]
print("\n1301 price_scaled range:")
print(f"  If /1e7:  {ticks3['price_scaled'].min()/1e7:.2f} .. {ticks3['price_scaled'].max()/1e7:.2f}")
