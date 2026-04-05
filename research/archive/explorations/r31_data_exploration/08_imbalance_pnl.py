"""R31-08: LOB imbalance alpha — P&L simulation.
Strong ICs found (0.07-0.15 at h=10).
Simulate actual P&L accounting for spread crossing costs.
"""
import pandas as pd
import numpy as np
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")

# Best candidates from imbalance scan
CANDIDATES = ["2881", "2454", "2308", "2474", "2891", "1301", "2330", "2317"]

def simulate_imbalance_alpha(sym, h=10, imb_threshold=0.3):
    """
    Strategy: When L5 imbalance > threshold, enter LONG (buy at ask, sell at bid after h updates).
    When L5 imbalance < -threshold, enter SHORT (sell at bid, buy at ask after h updates).
    P&L measured at bid/ask (not mid) to be realistic.
    """
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    all_pnl = []
    all_trades = []
    total_long = 0
    total_short = 0

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
        mid = (best_bid + best_ask) / 2

        l5bv = np.array([x[:5].sum() if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in bv], dtype=float)
        l5av = np.array([x[:5].sum() if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in av], dtype=float)
        l5t = l5bv + l5av
        imb = np.where(l5t > 0, (l5bv - l5av) / l5t, 0.0)

        valid = (best_bid > 0) & (best_ask > 0)
        if valid.sum() < h + 20:
            continue

        day_pnl = []
        day_trades = 0

        for i in range(len(ba) - h):
            if not valid[i] or not valid[i + h]:
                continue

            if imb[i] > imb_threshold:
                # Long signal: buy at ask[i], sell at bid[i+h]
                entry = best_ask[i]
                exit_price = best_bid[i + h]
                if entry > 0 and exit_price > 0:
                    pnl_bps = (exit_price - entry) / mid[i] * 10000
                    # Subtract commission: 1.425‰ buy + 1.425‰ sell + 3‰ tax = 5.85‰ = 5.85 bps
                    pnl_bps -= 5.85
                    day_pnl.append(pnl_bps)
                    day_trades += 1
                    total_long += 1

            elif imb[i] < -imb_threshold:
                # Short signal: sell at bid[i], buy at ask[i+h]
                entry = best_bid[i]
                exit_price = best_ask[i + h]
                if entry > 0 and exit_price > 0:
                    pnl_bps = (entry - exit_price) / mid[i] * 10000
                    pnl_bps -= 5.85  # commission
                    day_pnl.append(pnl_bps)
                    day_trades += 1
                    total_short += 1

        if day_pnl:
            all_pnl.extend(day_pnl)
            all_trades.append(day_trades)

    return np.array(all_pnl), all_trades, total_long, total_short


# Test various horizons and thresholds
print("=== LOB IMBALANCE ALPHA — P&L AFTER COSTS ===\n")
print(f"{'Symbol':8s} | {'h':>3s} | {'thresh':>6s} | {'mean_bps':>9s} | {'std_bps':>8s} | "
      f"{'sharpe':>7s} | {'win%':>5s} | {'N':>8s} | {'long':>6s} | {'short':>6s}")
print("-" * 95)

best_configs = []

for sym in CANDIDATES:
    for h in [5, 10, 20, 50]:
        for thresh in [0.2, 0.3, 0.5]:
            pnl, trades, n_long, n_short = simulate_imbalance_alpha(sym, h=h, imb_threshold=thresh)
            if len(pnl) < 50:
                continue
            mean_pnl = pnl.mean()
            std_pnl = pnl.std() if pnl.std() > 0 else 1
            sharpe = mean_pnl / std_pnl * np.sqrt(252 * len(trades) / len(set(f.stem for f in (DATA / sym).glob("*.parquet"))))
            win_rate = (pnl > 0).mean()

            if mean_pnl > 0:
                best_configs.append((sym, h, thresh, mean_pnl, std_pnl, sharpe, win_rate, len(pnl), n_long, n_short))

            print(f"{sym:8s} | {h:3d} | {thresh:6.1f} | {mean_pnl:9.2f} | {std_pnl:8.2f} | "
                  f"{sharpe:7.1f} | {win_rate:5.1%} | {len(pnl):8d} | {n_long:6d} | {n_short:6d}")

# Show best configs
print("\n\n=== BEST CONFIGURATIONS (positive net P&L after costs) ===")
best_configs.sort(key=lambda x: -x[3])
for sym, h, thresh, mean_pnl, std_pnl, sharpe, win_rate, n, nl, ns in best_configs[:10]:
    print(f"  {sym} h={h} thresh={thresh}: mean={mean_pnl:.2f}bps, win={win_rate:.1%}, "
          f"n={n}, long={nl}, short={ns}")


# === Passive MM variant: post limit orders, don't cross spread ===
print("\n\n=== PASSIVE VARIANT: Post at bid/ask, conditional on imbalance ===")
print("If imbalance > thresh: post buy limit at best_bid, exit at best_ask after h updates")
print("This earns the spread but risk of adverse selection.\n")

for sym in ["2881", "2454", "2891", "1301"]:
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    passive_pnl = []

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
        mid = (best_bid + best_ask) / 2

        l5bv = np.array([x[:5].sum() if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in bv], dtype=float)
        l5av = np.array([x[:5].sum() if isinstance(x, np.ndarray) and len(x) > 0 else 0 for x in av], dtype=float)
        l5t = l5bv + l5av
        imb = np.where(l5t > 0, (l5bv - l5av) / l5t, 0.0)

        valid = (best_bid > 0) & (best_ask > 0)
        h = 20

        for i in range(len(ba) - h):
            if not valid[i] or not valid[i + h]:
                continue

            if imb[i] > 0.3:
                # Passive buy at bid[i], passive sell at ask[i+h]
                # Assume fill at bid[i] (optimistic — queue position matters)
                entry = best_bid[i]
                exit_price = best_ask[i + h]
                if entry > 0 and exit_price > 0:
                    pnl_bps = (exit_price - entry) / mid[i] * 10000
                    pnl_bps -= 5.85  # still pay commission
                    passive_pnl.append(pnl_bps)

    if passive_pnl:
        pp = np.array(passive_pnl)
        print(f"  {sym}: passive_mean={pp.mean():.2f}bps, std={pp.std():.2f}, "
              f"win={(pp > 0).mean():.1%}, n={len(pp)}")
