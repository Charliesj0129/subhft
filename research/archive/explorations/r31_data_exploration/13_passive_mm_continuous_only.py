"""R31-13: Passive MM analysis — continuous session only (09:00-13:25).
Filter out opening auction (08:30-09:00) and closing auction (13:25-13:30).
Also de-duplicate ticks (every tick appears twice).
"""
import pandas as pd
import numpy as np
from pathlib import Path
from scipy.stats import spearmanr
import warnings
warnings.filterwarnings("ignore")

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")

CANDIDATES = ["2881", "2454", "2891", "1301", "2303", "2884", "1303",
              "2886", "2882", "2308", "2412", "2301", "5871", "3045"]

def filter_continuous_session(df):
    """Keep only continuous trading session (09:00-13:25 local = UTC+8)."""
    ts_s = df["exch_ts"].values / 1e9
    tod_s = (ts_s + 8 * 3600) % 86400
    continuous_start = 9 * 3600    # 09:00
    continuous_end = 13.4167 * 3600  # 13:25
    mask = (tod_s >= continuous_start) & (tod_s <= continuous_end)
    return df[mask].copy()

def dedup_ticks(df):
    """Remove duplicate ticks (each appears twice)."""
    return df.drop_duplicates(subset=["exch_ts", "price_scaled", "type"], keep="first")


def compute_imbalance_ic_continuous(sym, horizons=[5, 10, 20, 50]):
    """LOB imbalance IC using continuous session only."""
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    results = {h: {"ic_l1": [], "ic_l5": [], "n": 0} for h in horizons}

    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        df = filter_continuous_session(df)
        df = dedup_ticks(df)
        ba = df[df["type"] == "BidAsk"].sort_values("exch_ts").reset_index(drop=True)

        if len(ba) < 200:
            continue

        bp = ba["bids_price"].values
        bv = ba["bids_vol"].values
        ap = ba["asks_price"].values
        av = ba["asks_vol"].values

        best_bid = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in bp], dtype=float)
        best_ask = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in ap], dtype=float)

        l1bv = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in bv], dtype=float)
        l1av = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in av], dtype=float)
        l1t = l1bv + l1av
        l1_imb = np.where(l1t > 0, (l1bv - l1av) / l1t, 0.0)

        l5bv = np.array([x[:5].sum() if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in bv], dtype=float)
        l5av = np.array([x[:5].sum() if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in av], dtype=float)
        l5t = l5bv + l5av
        l5_imb = np.where(l5t > 0, (l5bv - l5av) / l5t, 0.0)

        mid = (best_bid + best_ask) / 2
        valid = (best_bid > 0) & (best_ask > 0) & np.isfinite(l1_imb) & np.isfinite(l5_imb)
        if valid.sum() < 100:
            continue

        mid_v = mid[valid]
        l1_v = l1_imb[valid]
        l5_v = l5_imb[valid]

        for h in horizons:
            if len(mid_v) <= h + 10:
                continue
            fwd_ret = np.log(mid_v[h:]) - np.log(mid_v[:-h])
            fvalid = np.isfinite(fwd_ret) & (np.abs(fwd_ret) < 0.05)
            if fvalid.sum() < 50:
                continue
            ic1, _ = spearmanr(l1_v[:-h][fvalid], fwd_ret[fvalid])
            ic5, _ = spearmanr(l5_v[:-h][fvalid], fwd_ret[fvalid])
            if np.isfinite(ic1) and np.isfinite(ic5):
                results[h]["ic_l1"].append(ic1)
                results[h]["ic_l5"].append(ic5)
                results[h]["n"] += fvalid.sum()

    return results


def simulate_passive_mm_continuous(sym, h=20, imb_thresh=0.3):
    """
    Passive MM during continuous session only.
    Entry: post at best_bid when L5 imb > thresh.
    Exit: post at best_ask after h BidAsk updates.
    Model fill using price-through heuristic.
    """
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    all_pnl = []
    total_attempted = 0
    n_days = 0

    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        df = filter_continuous_session(df)
        df = dedup_ticks(df)
        ba = df[df["type"] == "BidAsk"].sort_values("exch_ts").reset_index(drop=True)

        if len(ba) < 200:
            continue
        n_days += 1

        bp = ba["bids_price"].values
        bv = ba["bids_vol"].values
        ap = ba["asks_price"].values
        av = ba["asks_vol"].values

        best_bid = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in bp], dtype=float)
        best_ask = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in ap], dtype=float)
        mid = (best_bid + best_ask) / 2

        l5bv = np.array([x[:5].sum() if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in bv], dtype=float)
        l5av = np.array([x[:5].sum() if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in av], dtype=float)
        l5t = l5bv + l5av
        imb = np.where(l5t > 0, (l5bv - l5av) / l5t, 0.0)

        valid = (best_bid > 0) & (best_ask > 0)
        ts = ba["exch_ts"].values

        i = 0
        while i < len(ba) - h:
            if not valid[i]:
                i += 1
                continue

            if imb[i] > imb_thresh:
                total_attempted += 1
                entry_bid = best_bid[i]

                # Fill model: check if trade happens at or below our bid price
                filled = False
                for j in range(i+1, min(i+h+1, len(ba))):
                    if valid[j]:
                        if best_ask[j] <= entry_bid:
                            filled = True
                            break
                        if best_bid[j] < entry_bid:
                            # Bid retreated through our level
                            filled = True
                            break

                if filled:
                    # Exit attempt: post at ask after h updates from entry
                    exit_idx = min(i + h, len(ba) - 1)
                    if valid[exit_idx]:
                        exit_ask = best_ask[exit_idx]
                        if exit_ask > 0 and entry_bid > 0:
                            gross_bps = (exit_ask - entry_bid) / mid[i] * 10000
                            net_bps = gross_bps - 5.85
                            all_pnl.append(net_bps)

                i += h  # skip ahead after trade
            else:
                i += 1

    return np.array(all_pnl), total_attempted, n_days


# 1. IC analysis (continuous session only)
print("=== LOB IMBALANCE IC — CONTINUOUS SESSION ONLY ===\n")
print(f"{'Symbol':8s} | {'h':>3s} | {'L1_IC':>8s} | {'L5_IC':>8s} | {'t_L5':>6s} | {'N':>10s}")
print("-" * 60)

for sym in CANDIDATES[:8]:
    results = compute_imbalance_ic_continuous(sym)
    for h_val in [10, 20, 50]:
        r = results[h_val]
        if r["ic_l5"]:
            ml1 = np.mean(r["ic_l1"])
            ml5 = np.mean(r["ic_l5"])
            std5 = np.std(r["ic_l5"]) if len(r["ic_l5"]) > 1 else 1
            t5 = ml5 / std5 * np.sqrt(len(r["ic_l5"])) if std5 > 0 else 0
            print(f"{sym:8s} | {h_val:3d} | {ml1:8.4f} | {ml5:8.4f} | {t5:6.2f} | {r['n']:10d}")


# 2. Passive MM simulation (continuous session only)
print("\n\n=== PASSIVE MM — CONTINUOUS SESSION, REALISTIC FILL ===\n")
print(f"{'Symbol':8s} | {'mean_bps':>9s} | {'std':>8s} | {'win%':>5s} | {'filled':>8s} | "
      f"{'attempted':>10s} | {'fill%':>6s} | {'days':>4s} | {'fills/day':>9s}")
print("-" * 100)

viable = []
np.random.seed(42)
for sym in CANDIDATES:
    pnl, attempted, n_days = simulate_passive_mm_continuous(sym, h=20, imb_thresh=0.3)
    if len(pnl) < 10:
        continue
    fill_rate = len(pnl) / attempted if attempted > 0 else 0
    fills_per_day = len(pnl) / n_days if n_days > 0 else 0
    mean_pnl = pnl.mean()
    std_pnl = pnl.std()
    win = (pnl > 0).mean()

    # Daily P&L estimate
    daily_gross = mean_pnl * fills_per_day  # in bps on avg trade
    daily_std = std_pnl * np.sqrt(fills_per_day)

    print(f"{sym:8s} | {mean_pnl:9.2f} | {std_pnl:8.2f} | {win:5.1%} | {len(pnl):8d} | "
          f"{attempted:10d} | {fill_rate:6.1%} | {n_days:4d} | {fills_per_day:9.0f}")

    if mean_pnl > 0:
        viable.append({
            "sym": sym,
            "mean_bps": mean_pnl,
            "std_bps": std_pnl,
            "win_rate": win,
            "fills": len(pnl),
            "fills_per_day": fills_per_day,
            "daily_gross_bps": daily_gross,
        })


# 3. Summary of viable candidates
print("\n\n=== VIABLE PASSIVE MM CANDIDATES ===")
if viable:
    viable.sort(key=lambda x: -x["daily_gross_bps"])
    for v in viable:
        sharpe_est = v["mean_bps"] / v["std_bps"] * np.sqrt(252 * v["fills_per_day"]) if v["std_bps"] > 0 else 0
        print(f"  {v['sym']:8s}: mean={v['mean_bps']:.1f}bps/trade, {v['fills_per_day']:.0f} fills/day, "
              f"daily_gross={v['daily_gross_bps']:.0f}bps, win={v['win_rate']:.0%}, "
              f"sharpe_est={sharpe_est:.1f}")
else:
    print("  None")


# 4. Sensitivity to threshold
print("\n\n=== SENSITIVITY TO IMB THRESHOLD (best symbols) ===")
best_syms = [v["sym"] for v in viable[:4]] if viable else CANDIDATES[:4]
for sym in best_syms:
    for thresh in [0.1, 0.2, 0.3, 0.5, 0.7]:
        pnl, attempted, n_days = simulate_passive_mm_continuous(sym, h=20, imb_thresh=thresh)
        if len(pnl) < 10:
            continue
        fills_per_day = len(pnl) / n_days if n_days > 0 else 0
        print(f"  {sym} thresh={thresh:.1f}: mean={pnl.mean():.1f}bps, "
              f"fills/day={fills_per_day:.0f}, win={(pnl>0).mean():.0%}, n={len(pnl)}")


# 5. Sensitivity to holding period
print("\n\n=== SENSITIVITY TO HOLDING PERIOD (best symbols) ===")
for sym in best_syms[:3]:
    for hold in [5, 10, 20, 50, 100]:
        pnl, attempted, n_days = simulate_passive_mm_continuous(sym, h=hold, imb_thresh=0.3)
        if len(pnl) < 10:
            continue
        fills_per_day = len(pnl) / n_days if n_days > 0 else 0
        print(f"  {sym} hold={hold:3d}: mean={pnl.mean():.1f}bps, "
              f"fills/day={fills_per_day:.0f}, win={(pnl>0).mean():.0%}, n={len(pnl)}")
