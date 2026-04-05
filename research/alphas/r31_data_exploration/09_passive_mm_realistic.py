"""R31-09: Passive MM with LOB imbalance — realistic fill model.
The passive variant showed strong results but assumed instant fill.
Here we model queue position and adverse selection more carefully.
"""
import pandas as pd
import numpy as np
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")

CANDIDATES = ["2881", "2454", "2891", "1301", "2886", "2882", "2884", "1303", "2303"]

def simulate_passive_mm(sym, h=20, imb_thresh=0.3, fill_prob=0.5):
    """
    Passive MM: post at best bid when imb > thresh.
    Exit at best ask after h BidAsk updates (passive exit too).
    Model fill probability as fill_prob (rough approximation).
    Track adverse selection: when we get filled, did price move against us?
    """
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    all_pnl = []
    adverse_count = 0
    favorable_count = 0
    total_attempted = 0

    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        ba = df[df["type"] == "BidAsk"].copy().sort_values("exch_ts").reset_index(drop=True)
        if len(ba) < 200:
            continue

        bp = ba["bids_price"].values
        bv = ba["bids_vol"].values
        ap = ba["asks_price"].values
        av = ba["asks_vol"].values

        best_bid = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in bp], dtype=float)
        best_ask = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in ap], dtype=float)
        bid_vol = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in bv], dtype=float)
        mid = (best_bid + best_ask) / 2

        l5bv = np.array([x[:5].sum() if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in bv], dtype=float)
        l5av = np.array([x[:5].sum() if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in av], dtype=float)
        l5t = l5bv + l5av
        imb = np.where(l5t > 0, (l5bv - l5av) / l5t, 0.0)

        valid = (best_bid > 0) & (best_ask > 0)

        for i in range(len(ba) - h):
            if not valid[i] or not valid[i + h]:
                continue

            if imb[i] > imb_thresh:
                total_attempted += 1

                # Model fill probability based on volume at best bid
                # If we're at the back of the queue, fill is less likely
                # Simple model: fill if price touches our level within h updates
                entry_bid = best_bid[i]

                # Check if price drops to or below our bid in [i+1, i+h]
                # (someone sells at market into our limit order)
                filled = False
                for j in range(i+1, min(i+h+1, len(ba))):
                    if valid[j] and best_bid[j] < entry_bid:
                        # Price dropped through our level -> we likely got filled
                        filled = True
                        break
                    if valid[j] and best_ask[j] <= entry_bid:
                        # Ask dropped to our bid -> definite fill
                        filled = True
                        break

                if not filled:
                    # Use random fill model as backup
                    if np.random.random() < fill_prob * 0.3:  # reduced fill rate when price doesn't move
                        filled = True

                if filled:
                    # Exit: try to sell at ask after h updates
                    exit_price = best_ask[i + h]
                    if exit_price > 0 and entry_bid > 0:
                        gross_pnl = (exit_price - entry_bid) / mid[i] * 10000
                        # Commission only (no spread cost since we're passive both sides)
                        net_pnl = gross_pnl - 5.85
                        all_pnl.append(net_pnl)

                        if exit_price < entry_bid:
                            adverse_count += 1
                        else:
                            favorable_count += 1

    return np.array(all_pnl), total_attempted, adverse_count, favorable_count


# Also analyze spread characteristics more carefully
print("=== SPREAD ANALYSIS FOR MM CANDIDATES ===\n")
for sym in CANDIDATES:
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    all_spreads = []
    all_spread_changes = []
    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        ba = df[df["type"] == "BidAsk"].sort_values("exch_ts")
        if len(ba) < 100:
            continue
        bp = ba["bids_price"].values
        ap = ba["asks_price"].values
        best_bid = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in bp], dtype=float)
        best_ask = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in ap], dtype=float)
        mid = (best_bid + best_ask) / 2
        spread = best_ask - best_bid
        valid = (best_bid > 0) & (best_ask > 0)
        spread_v = spread[valid]
        mid_v = mid[valid]
        spread_bps = spread_v / mid_v * 10000
        all_spreads.extend(spread_bps.tolist())

        # Tick size relative to price
        if len(mid_v) > 0:
            price_ntd = mid_v[0] / 1e7  # rough NTD conversion
            # TWSE tick sizes vary by price level
            if price_ntd < 10:
                tick_size = 0.01
            elif price_ntd < 50:
                tick_size = 0.05
            elif price_ntd < 100:
                tick_size = 0.1
            elif price_ntd < 500:
                tick_size = 0.5
            elif price_ntd < 1000:
                tick_size = 1.0
            else:
                tick_size = 5.0

    if all_spreads:
        sp = np.array(all_spreads)
        print(f"{sym:6s}: median_spread={np.median(sp):.1f}bps, mean={sp.mean():.1f}bps, "
              f"p10={np.percentile(sp, 10):.1f}, p90={np.percentile(sp, 90):.1f}, "
              f"1-tick%={(sp == np.min(sp[sp > 0])).mean()*100:.1f}%")


# Run realistic simulation
print("\n\n=== PASSIVE MM — REALISTIC FILL MODEL ===\n")
print(f"{'Symbol':8s} | {'mean_bps':>9s} | {'std_bps':>8s} | {'win%':>5s} | {'filled':>8s} | "
      f"{'attempted':>10s} | {'fill_rate':>9s} | {'adv_sel':>8s}")
print("-" * 95)

np.random.seed(42)
for sym in CANDIDATES:
    pnl, attempted, adverse, favorable = simulate_passive_mm(sym, h=20, imb_thresh=0.3)
    if len(pnl) < 10:
        continue
    fill_rate = len(pnl) / attempted if attempted > 0 else 0
    adv_rate = adverse / len(pnl) if len(pnl) > 0 else 0
    print(f"{sym:8s} | {pnl.mean():9.2f} | {pnl.std():8.2f} | {(pnl > 0).mean():5.1%} | "
          f"{len(pnl):8d} | {attempted:10d} | {fill_rate:9.1%} | {adv_rate:8.1%}")


# === Alternative: check wider-spread stocks for passive MM ===
print("\n\n=== WIDER-SPREAD STOCKS — PASSIVE MM ===")
WIDE_SPREAD = ["2345", "3045", "5871", "2308", "2412", "2301"]
for sym in WIDE_SPREAD:
    pnl, attempted, adverse, favorable = simulate_passive_mm(sym, h=20, imb_thresh=0.3)
    if len(pnl) < 10:
        continue
    fill_rate = len(pnl) / attempted if attempted > 0 else 0
    print(f"{sym:8s} | {pnl.mean():9.2f} | {pnl.std():8.2f} | {(pnl > 0).mean():5.1%} | "
          f"{len(pnl):8d} | {attempted:10d} | {fill_rate:9.1%}")
