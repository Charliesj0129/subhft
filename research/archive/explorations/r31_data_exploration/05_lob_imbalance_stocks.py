"""R31-05: LOB imbalance predictability for stocks.
Does L5 order book imbalance predict next-N-tick returns for individual stocks?
"""
import pandas as pd
import numpy as np
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")

# Focus on the most liquid stocks
STOCKS = ["2330", "2317", "2303", "2454", "2382", "2412", "2881", "2886", "2891",
          "1301", "2308", "2474", "3034", "2327"]

def compute_imbalance_alpha(sym, horizons_ticks=[1, 5, 10, 20, 50, 100]):
    """Compute LOB imbalance -> forward return predictability."""
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    all_results = {h: {"ic": [], "n": 0} for h in horizons_ticks}

    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")

        # Separate ticks and BidAsk
        ba = df[df["type"] == "BidAsk"].copy().sort_values("exch_ts").reset_index(drop=True)
        ticks = df[df["type"] == "Tick"].copy().sort_values("exch_ts").reset_index(drop=True)

        if len(ba) < 200 or len(ticks) < 200:
            continue

        # Compute L5 imbalance from BidAsk rows
        def compute_imb(row):
            bids = row["bids_vol"]
            asks = row["asks_vol"]
            if not isinstance(bids, list) or not isinstance(asks, list):
                return np.nan
            bid_vol = sum(bids[:5]) if bids else 0
            ask_vol = sum(asks[:5]) if asks else 0
            total = bid_vol + ask_vol
            if total == 0:
                return 0.0
            return (bid_vol - ask_vol) / total

        ba["imbalance"] = ba.apply(compute_imb, axis=1)
        ba = ba.dropna(subset=["imbalance"])

        if len(ba) < 100:
            continue

        # L1 imbalance
        def compute_l1_imb(row):
            bids = row["bids_vol"]
            asks = row["asks_vol"]
            if not isinstance(bids, list) or not isinstance(asks, list) or len(bids) == 0 or len(asks) == 0:
                return np.nan
            return (bids[0] - asks[0]) / (bids[0] + asks[0]) if (bids[0] + asks[0]) > 0 else 0

        ba["l1_imb"] = ba.apply(compute_l1_imb, axis=1)

        # Get mid price from BidAsk
        def get_mid(row):
            bids_p = row["bids_price"]
            asks_p = row["asks_price"]
            if isinstance(bids_p, list) and isinstance(asks_p, list) and len(bids_p) > 0 and len(asks_p) > 0:
                return (bids_p[0] + asks_p[0]) / 2
            return np.nan

        ba["mid"] = ba.apply(get_mid, axis=1)
        ba = ba.dropna(subset=["mid"])
        ba = ba[ba["mid"] > 0].reset_index(drop=True)

        if len(ba) < 100:
            continue

        mid_arr = ba["mid"].values.astype(float)
        imb_arr = ba["imbalance"].values
        l1_imb_arr = ba["l1_imb"].values

        # Forward returns at various horizons (in ticks of BidAsk updates)
        for h in horizons_ticks:
            if len(mid_arr) <= h + 10:
                continue
            fwd_ret = np.log(mid_arr[h:]) - np.log(mid_arr[:-h])
            imb = imb_arr[:-h]
            l1 = l1_imb_arr[:-h]

            # Filter valid
            valid = np.isfinite(fwd_ret) & np.isfinite(imb) & (np.abs(fwd_ret) < 0.05)
            if valid.sum() < 50:
                continue

            # Rank IC (Spearman)
            from scipy.stats import spearmanr
            ic_l5, _ = spearmanr(imb[valid], fwd_ret[valid])
            ic_l1, _ = spearmanr(l1[valid], fwd_ret[valid])

            all_results[h]["ic"].append((ic_l1, ic_l5, valid.sum()))
            all_results[h]["n"] += valid.sum()

    return all_results


print("=== LOB IMBALANCE -> FORWARD RETURN IC ===")
print(f"{'Symbol':8s} | {'Horizon':>8s} | {'L1_IC':>8s} | {'L5_IC':>8s} | {'t_L5':>8s} | {'N':>10s}")
print("-" * 70)

summary = {}
for sym in STOCKS:
    results = compute_imbalance_alpha(sym)
    summary[sym] = results
    for h in [1, 5, 10, 20, 50, 100]:
        if results[h]["ic"]:
            l1_ics = [x[0] for x in results[h]["ic"]]
            l5_ics = [x[1] for x in results[h]["ic"]]
            mean_l1 = np.mean(l1_ics)
            mean_l5 = np.mean(l5_ics)
            std_l5 = np.std(l5_ics) if len(l5_ics) > 1 else 1
            t_stat = mean_l5 / std_l5 * np.sqrt(len(l5_ics)) if std_l5 > 0 else 0
            n = results[h]["n"]
            print(f"{sym:8s} | {h:8d} | {mean_l1:8.4f} | {mean_l5:8.4f} | {t_stat:8.2f} | {n:10d}")

# Identify best candidates
print("\n\n=== BEST IMBALANCE ALPHA CANDIDATES (h=10, sorted by |L5_IC|) ===")
candidates = []
for sym in STOCKS:
    r = summary.get(sym, {}).get(10, {})
    if r and r.get("ic"):
        l5_ics = [x[1] for x in r["ic"]]
        mean_ic = np.mean(l5_ics)
        candidates.append((sym, mean_ic, r["n"]))

candidates.sort(key=lambda x: -abs(x[1]))
for sym, ic, n in candidates:
    print(f"  {sym}: IC={ic:.4f}, N={n}")

# Check if imbalance predicts DIRECTION (not just rank)
print("\n\n=== DIRECTIONAL ACCURACY (L5 imb sign -> return sign, h=10) ===")
for sym in STOCKS[:5]:
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    correct, total = 0, 0
    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        ba = df[df["type"] == "BidAsk"].copy().sort_values("exch_ts").reset_index(drop=True)
        if len(ba) < 200:
            continue
        def compute_imb(row):
            bids = row["bids_vol"]
            asks = row["asks_vol"]
            if not isinstance(bids, list) or not isinstance(asks, list):
                return np.nan
            bv = sum(bids[:5]) if bids else 0
            av = sum(asks[:5]) if asks else 0
            t = bv + av
            return (bv - av) / t if t > 0 else 0
        def get_mid(row):
            bp = row["bids_price"]
            ap = row["asks_price"]
            if isinstance(bp, list) and isinstance(ap, list) and len(bp) > 0 and len(ap) > 0:
                return (bp[0] + ap[0]) / 2
            return np.nan
        ba["imb"] = ba.apply(compute_imb, axis=1)
        ba["mid"] = ba.apply(get_mid, axis=1)
        ba = ba.dropna(subset=["imb", "mid"])
        ba = ba[ba["mid"] > 0].reset_index(drop=True)
        if len(ba) < 20:
            continue
        mid = ba["mid"].values.astype(float)
        imb = ba["imb"].values
        h = 10
        fwd = mid[h:] - mid[:-h]
        im = imb[:-h]
        valid = np.isfinite(fwd) & np.isfinite(im) & (im != 0)
        c = ((np.sign(im[valid]) == np.sign(fwd[valid])).sum())
        correct += c
        total += valid.sum()

    if total > 0:
        print(f"  {sym}: accuracy={correct/total:.3f}, n={total}")
