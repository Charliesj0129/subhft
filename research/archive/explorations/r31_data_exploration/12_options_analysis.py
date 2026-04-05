"""R31-12: Options-futures analysis.
Put-call parity violations, vol surface dynamics, options-futures basis.
"""
import pandas as pd
import numpy as np
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")

# Get all TXO options
opt_dirs = sorted([d.name for d in DATA.iterdir() if d.name.startswith("TXO")])
print(f"TXO option symbols: {len(opt_dirs)}")

# Parse option info from symbol names
# Format: TXO<strike><type><month>  e.g., TXO22400B6 = strike 22400, month B(Feb), type 6(?)
# Actually: TXO<strike><expiry_code>
# C6 = Call March, O6 = Put March (O = Put in TAIFEX convention)
# B6 = Feb, N6 = Feb near... complex naming

# Let's first understand the naming
print("\nSample option names:")
for d in opt_dirs[:20]:
    print(f"  {d}")
print("...")
for d in opt_dirs[-10:]:
    print(f"  {d}")

# Parse: last 2 chars = expiry code, second-to-last char before that = call/put
# C = Call, P/O = Put in different conventions
# Let's check which have data
print("\n=== OPTIONS DATA AVAILABILITY ===")
opt_info = []
for d in opt_dirs:
    name = d  # e.g. TXO22400C6
    # Parse strike: digits after TXO
    strike_str = ""
    rest = name[3:]  # "22400C6"
    for c in rest:
        if c.isdigit():
            strike_str += c
        else:
            break
    suffix = rest[len(strike_str):]  # "C6" or "O6" or "B6" etc

    if not strike_str:
        continue

    strike = int(strike_str)

    # C6 = Call March, O6 = Put March
    # B6 = ? Let's see what data looks like
    is_call = 'C' in suffix
    is_put = 'O' in suffix or 'P' in suffix

    files = sorted((DATA / d).glob("*.parquet"))
    dates = [f.stem for f in files]
    n_dates = len(dates)

    if n_dates > 0:
        opt_info.append({
            "symbol": d,
            "strike": strike,
            "suffix": suffix,
            "is_call": is_call,
            "is_put": is_put,
            "n_dates": n_dates,
            "dates": dates,
        })

print(f"\nOptions with data: {len(opt_info)}")

# Group by call/put
calls = [o for o in opt_info if o["is_call"]]
puts = [o for o in opt_info if o["is_put"]]
other = [o for o in opt_info if not o["is_call"] and not o["is_put"]]
print(f"Calls: {len(calls)}, Puts: {len(puts)}, Other: {len(other)}")

# Show strike range for calls and puts
if calls:
    print(f"Call strikes: {min(o['strike'] for o in calls)} - {max(o['strike'] for o in calls)}")
if puts:
    print(f"Put strikes: {min(o['strike'] for o in puts)} - {max(o['strike'] for o in puts)}")

# Check a sample option's data
if opt_info:
    sample = opt_info[len(opt_info)//2]
    print(f"\nSample option: {sample['symbol']}")
    fp = DATA / sample["symbol"] / f"{sample['dates'][0]}.parquet"
    df = pd.read_parquet(fp)
    print(f"  Rows: {len(df)}")
    print(f"  Types: {df['type'].unique()}")
    ticks = df[df["type"] == "Tick"]
    ba = df[df["type"].isin(["BidAsk", "Snapshot"])]
    print(f"  Ticks: {len(ticks)}, BidAsk/Snapshot: {len(ba)}")
    if len(ticks) > 0:
        print(f"  Price range: {ticks['price_scaled'].min()} - {ticks['price_scaled'].max()}")
        print(f"  Volume range: {ticks['volume'].min()} - {ticks['volume'].max()}")
    print(f"  First few rows:")
    print(df.head(5)[["symbol", "type", "exch_ts", "price_scaled", "volume"]].to_string())


# === Put-Call Parity Check ===
print("\n\n=== PUT-CALL PARITY ANALYSIS ===")
# P-C parity: C - P = S - K*e^(-rT) (approximately C - P ≈ F - K for futures options)
# For each matching call/put pair, check the parity deviation

# Find matching pairs (same strike, same expiry)
call_map = {(o["strike"], o["suffix"]): o for o in calls}
put_map = {}
for o in puts:
    # O6 pairs with C6 (same month)
    call_suffix = o["suffix"].replace("O", "C").replace("P", "C")
    key = (o["strike"], call_suffix)
    put_map[key] = o

pairs = []
for key in call_map:
    if key in put_map:
        pairs.append((call_map[key], put_map[key]))

print(f"Matched call-put pairs: {len(pairs)}")

# Load TXF for forward price
# Use TXFC6 or TXFD6 as forward
txf_sym = None
for s in ["TXFC6", "TXFD6"]:
    if (DATA / s).exists():
        txf_sym = s
        break

if txf_sym and pairs:
    # For each date, compute parity deviation
    for pair_idx, (call_opt, put_opt) in enumerate(pairs[:5]):  # check first 5 pairs
        common_dates = set(call_opt["dates"]) & set(put_opt["dates"])
        txf_dates = set(f.stem for f in (DATA / txf_sym).glob("*.parquet"))
        common_dates = sorted(common_dates & txf_dates)

        if not common_dates:
            continue

        print(f"\n  Pair: {call_opt['symbol']} / {put_opt['symbol']} (K={call_opt['strike']})")

        for date_str in common_dates[:2]:
            # Load call ticks
            c_df = pd.read_parquet(DATA / call_opt["symbol"] / f"{date_str}.parquet")
            p_df = pd.read_parquet(DATA / put_opt["symbol"] / f"{date_str}.parquet")
            f_df = pd.read_parquet(DATA / txf_sym / f"{date_str}.parquet")

            c_ticks = c_df[c_df["type"] == "Tick"]
            p_ticks = p_df[p_df["type"] == "Tick"]
            f_ticks = f_df[f_df["type"] == "Tick"]

            if len(c_ticks) < 10 or len(p_ticks) < 10 or len(f_ticks) < 10:
                print(f"    {date_str}: insufficient data (C={len(c_ticks)}, P={len(p_ticks)}, F={len(f_ticks)})")
                continue

            # Use median prices as rough estimate
            c_price = c_ticks["price_scaled"].median() / 1e6  # TXO price in points
            p_price = p_ticks["price_scaled"].median() / 1e6
            f_price = f_ticks["price_scaled"].median() / 1e6  # TXF in index points

            # Put-call parity for futures options: C - P = (F - K) * discount
            # Approximate: C - P ≈ F - K (ignoring discount for near expiry)
            K = call_opt["strike"]
            parity_diff = c_price - p_price - (f_price - K)

            print(f"    {date_str}: C={c_price:.0f}, P={p_price:.0f}, F={f_price:.0f}, K={K}, "
                  f"C-P={c_price-p_price:.0f}, F-K={f_price-K:.0f}, "
                  f"parity_dev={parity_diff:.0f} pts ({parity_diff * 50:.0f} NTD)")


# === Options liquidity check ===
print("\n\n=== OPTIONS LIQUIDITY CHECK ===")
for o in sorted(opt_info, key=lambda x: -x["n_dates"])[:20]:
    total_vol = 0
    total_ticks = 0
    for date_str in o["dates"]:
        df = pd.read_parquet(DATA / o["symbol"] / f"{date_str}.parquet")
        ticks = df[df["type"] == "Tick"]
        total_vol += ticks["volume"].sum()
        total_ticks += len(ticks)

    avg_vol = total_vol / o["n_dates"] if o["n_dates"] > 0 else 0
    avg_ticks = total_ticks / o["n_dates"] if o["n_dates"] > 0 else 0
    print(f"  {o['symbol']:14s}: {o['n_dates']:2d} days, avg_vol/day={avg_vol:8.0f}, "
          f"avg_ticks/day={avg_ticks:6.0f}")
