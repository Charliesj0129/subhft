"""R31-05v2: LOB imbalance predictability for stocks.
Fixed: numpy arrays not lists for bid/ask data.
"""
import pandas as pd
import numpy as np
from pathlib import Path
from scipy.stats import spearmanr
import warnings
warnings.filterwarnings("ignore")

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")

STOCKS = ["2330", "2317", "2303", "2454", "2382", "2412", "2881", "2886", "2891",
          "1301", "2308", "2474", "3034", "2327"]

def compute_imbalance_alpha(sym, horizons=[1, 5, 10, 20, 50, 100]):
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    all_results = {h: {"ic_l1": [], "ic_l5": [], "n": 0} for h in horizons}

    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        ba = df[df["type"] == "BidAsk"].copy().sort_values("exch_ts").reset_index(drop=True)

        if len(ba) < 200:
            continue

        bp = ba["bids_price"].values
        bv = ba["bids_vol"].values
        ap = ba["asks_price"].values
        av = ba["asks_vol"].values

        # Vectorized extraction
        best_bid = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in bp], dtype=float)
        best_ask = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in ap], dtype=float)

        # L1 imbalance
        l1_bid_v = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in bv], dtype=float)
        l1_ask_v = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in av], dtype=float)
        l1_total = l1_bid_v + l1_ask_v
        l1_imb = np.where(l1_total > 0, (l1_bid_v - l1_ask_v) / l1_total, 0.0)

        # L5 imbalance
        l5_bid_v = np.array([x[:5].sum() if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in bv], dtype=float)
        l5_ask_v = np.array([x[:5].sum() if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in av], dtype=float)
        l5_total = l5_bid_v + l5_ask_v
        l5_imb = np.where(l5_total > 0, (l5_bid_v - l5_ask_v) / l5_total, 0.0)

        # Mid price
        mid = (best_bid + best_ask) / 2.0
        valid_mid = (best_bid > 0) & (best_ask > 0)
        mid[~valid_mid] = np.nan

        # Filter to valid rows
        mask = valid_mid & np.isfinite(l1_imb) & np.isfinite(l5_imb)
        if mask.sum() < 100:
            continue

        mid_v = mid[mask]
        l1_v = l1_imb[mask]
        l5_v = l5_imb[mask]

        for h in horizons:
            if len(mid_v) <= h + 10:
                continue
            fwd_ret = np.log(mid_v[h:]) - np.log(mid_v[:-h])
            imb_l1 = l1_v[:-h]
            imb_l5 = l5_v[:-h]

            fvalid = np.isfinite(fwd_ret) & (np.abs(fwd_ret) < 0.05)
            if fvalid.sum() < 50:
                continue

            ic1, _ = spearmanr(imb_l1[fvalid], fwd_ret[fvalid])
            ic5, _ = spearmanr(imb_l5[fvalid], fwd_ret[fvalid])

            if np.isfinite(ic1) and np.isfinite(ic5):
                all_results[h]["ic_l1"].append(ic1)
                all_results[h]["ic_l5"].append(ic5)
                all_results[h]["n"] += fvalid.sum()

    return all_results


print("=== LOB IMBALANCE -> FORWARD RETURN IC ===")
print(f"{'Symbol':8s} | {'Horizon':>8s} | {'L1_IC':>8s} | {'L5_IC':>8s} | {'t_L1':>8s} | {'t_L5':>8s} | {'N':>10s}")
print("-" * 80)

summary = {}
for sym in STOCKS:
    results = compute_imbalance_alpha(sym)
    summary[sym] = results
    for h in [1, 5, 10, 20, 50, 100]:
        r = results[h]
        if r["ic_l1"]:
            mean_l1 = np.mean(r["ic_l1"])
            mean_l5 = np.mean(r["ic_l5"])
            std_l1 = np.std(r["ic_l1"]) if len(r["ic_l1"]) > 1 else 1
            std_l5 = np.std(r["ic_l5"]) if len(r["ic_l5"]) > 1 else 1
            t1 = mean_l1 / std_l1 * np.sqrt(len(r["ic_l1"])) if std_l1 > 0 else 0
            t5 = mean_l5 / std_l5 * np.sqrt(len(r["ic_l5"])) if std_l5 > 0 else 0
            n = r["n"]
            print(f"{sym:8s} | {h:8d} | {mean_l1:8.4f} | {mean_l5:8.4f} | {t1:8.2f} | {t5:8.2f} | {n:10d}")

# Best candidates summary
print("\n\n=== BEST CANDIDATES (h=10, sorted by |L5_IC|) ===")
candidates = []
for sym in STOCKS:
    r = summary.get(sym, {}).get(10, {})
    if r and r.get("ic_l5"):
        mean_ic = np.mean(r["ic_l5"])
        candidates.append((sym, mean_ic, r["n"]))

candidates.sort(key=lambda x: -abs(x[1]))
for sym, ic, n in candidates:
    print(f"  {sym}: IC={ic:.4f}, N={n}")

# Directional accuracy
print("\n\n=== DIRECTIONAL ACCURACY (L5 imb sign -> ret sign, h=10) ===")
for sym in STOCKS[:8]:
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    correct, total = 0, 0
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
        l5bv = np.array([x[:5].sum() if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in bv], dtype=float)
        l5av = np.array([x[:5].sum() if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in av], dtype=float)
        l5t = l5bv + l5av
        imb = np.where(l5t > 0, (l5bv - l5av) / l5t, 0.0)
        mid = (best_bid + best_ask) / 2
        valid_mid = (best_bid > 0) & (best_ask > 0)
        mid[~valid_mid] = np.nan

        mask = valid_mid & np.isfinite(imb)
        mid_v = mid[mask]
        imb_v = imb[mask]
        h = 10
        if len(mid_v) <= h:
            continue
        fwd = mid_v[h:] - mid_v[:-h]
        im = imb_v[:-h]
        v = np.isfinite(fwd) & (im != 0)
        correct += int((np.sign(im[v]) == np.sign(fwd[v])).sum())
        total += int(v.sum())

    if total > 0:
        acc = correct / total
        edge_bps = (acc - 0.5) * 2  # rough edge per trade in "hit rate terms"
        print(f"  {sym}: accuracy={acc:.4f}, n={total}, edge_pct={edge_bps*100:.2f}%")
