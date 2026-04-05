"""Debug: check actual types of bids_price, bids_vol columns."""
import pandas as pd
from pathlib import Path
import json

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")
df = pd.read_parquet(DATA / "2330" / "2026-03-23.parquet")
ba = df[df["type"] == "BidAsk"].head(5)

for i, row in ba.iterrows():
    bp = row["bids_price"]
    bv = row["bids_vol"]
    ap = row["asks_price"]
    av = row["asks_vol"]
    print(f"Row {i}: bids_price type={type(bp)}, val={bp}")
    print(f"        bids_vol  type={type(bv)}, val={bv}")
    print(f"        asks_price type={type(ap)}, val={ap}")
    print(f"        asks_vol  type={type(av)}, val={av}")
    # Try to parse if string
    if isinstance(bp, str):
        try:
            parsed = json.loads(bp)
            print(f"        PARSED bids_price: {parsed}")
        except:
            print(f"        CANNOT PARSE")
    print()
