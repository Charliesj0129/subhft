"""R31-10: VWAP reversion P&L simulation.
Strong negative IC found (-0.16 to -0.29). Simulate actual trading P&L.
"""
import pandas as pd
import numpy as np
from pathlib import Path
from scipy.stats import spearmanr
import warnings
warnings.filterwarnings("ignore")

DATA = Path("/home/charlie/hft_platform/research/data/real/golden")

STOCKS = ["2330", "2317", "2303", "2454", "2382", "2412", "2881", "2886",
          "1301", "1303", "2891", "2882", "2884", "2308"]

def simulate_vwap_reversion(sym, lookback_ticks=200, hold_ticks=50, threshold_bps=5.0):
    """
    Strategy: When price deviates > threshold from VWAP, trade towards VWAP.
    Buy at ask when below VWAP by threshold, sell at bid when above VWAP by threshold.
    Hold for hold_ticks then exit at market (crossing spread).
    """
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    all_pnl = []
    all_ic = []
    trade_count = 0

    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        ticks = df[df["type"] == "Tick"].sort_values("exch_ts").reset_index(drop=True)
        ba = df[df["type"] == "BidAsk"].sort_values("exch_ts").reset_index(drop=True)

        if len(ticks) < 500 or len(ba) < 200:
            continue

        prices = ticks["price_scaled"].values.astype(float)
        volumes = ticks["volume"].values.astype(float)
        ts_ticks = ticks["exch_ts"].values

        # Get bid/ask for realistic execution
        bp = ba["bids_price"].values
        ap = ba["asks_price"].values
        best_bid_ba = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in bp], dtype=float)
        best_ask_ba = np.array([x[0] if isinstance(x, np.ndarray) and len(x) > 0 else np.nan for x in ap], dtype=float)
        ts_ba = ba["exch_ts"].values

        # Compute rolling VWAP
        cum_pv = np.cumsum(prices * volumes)
        cum_v = np.cumsum(volumes)
        vwap = cum_pv / np.maximum(cum_v, 1)

        # IC check first
        for start in range(lookback_ticks, len(prices) - hold_ticks, hold_ticks):
            dev = (prices[start] - vwap[start]) / vwap[start] * 10000  # bps
            fwd_ret = (prices[start + hold_ticks] - prices[start]) / prices[start] * 10000  # bps

            if np.isfinite(dev) and np.isfinite(fwd_ret):
                all_ic.append((dev, fwd_ret))

        # P&L simulation with bid/ask execution
        i = lookback_ticks
        while i < len(prices) - hold_ticks:
            dev_bps = (prices[i] - vwap[i]) / vwap[i] * 10000

            if abs(dev_bps) > threshold_bps:
                # Find closest bid/ask quote
                ba_idx = np.searchsorted(ts_ba, ts_ticks[i], side="right") - 1
                ba_exit_idx = np.searchsorted(ts_ba, ts_ticks[i + hold_ticks], side="right") - 1

                if 0 <= ba_idx < len(best_bid_ba) and 0 <= ba_exit_idx < len(best_bid_ba):
                    entry_bid = best_bid_ba[ba_idx]
                    entry_ask = best_ask_ba[ba_idx]
                    exit_bid = best_bid_ba[ba_exit_idx]
                    exit_ask = best_ask_ba[ba_exit_idx]

                    if np.isfinite(entry_bid) and np.isfinite(entry_ask) and \
                       np.isfinite(exit_bid) and np.isfinite(exit_ask) and \
                       entry_bid > 0 and entry_ask > 0 and exit_bid > 0 and exit_ask > 0:

                        entry_mid = (entry_bid + entry_ask) / 2

                        if dev_bps < -threshold_bps:
                            # Below VWAP: BUY at ask, sell at bid later
                            pnl_bps = (exit_bid - entry_ask) / entry_mid * 10000
                            pnl_bps -= 5.85  # commission
                            all_pnl.append(pnl_bps)
                            trade_count += 1
                        elif dev_bps > threshold_bps:
                            # Above VWAP: SELL at bid, buy at ask later
                            pnl_bps = (entry_bid - exit_ask) / entry_mid * 10000
                            pnl_bps -= 5.85  # commission
                            all_pnl.append(pnl_bps)
                            trade_count += 1

                i += hold_ticks  # skip ahead
            else:
                i += 1

    return np.array(all_pnl), all_ic, trade_count


print("=== VWAP REVERSION — AGGRESSIVE EXECUTION P&L ===\n")
print(f"{'Symbol':8s} | {'thresh':>6s} | {'hold':>5s} | {'mean_bps':>9s} | {'std':>8s} | "
      f"{'win%':>5s} | {'sharpe_d':>8s} | {'N':>8s} | {'IC':>8s}")
print("-" * 90)

best_configs = []

for sym in STOCKS:
    for thresh in [3.0, 5.0, 10.0, 20.0]:
        for hold in [20, 50, 100, 200]:
            pnl, ic_pairs, n_trades = simulate_vwap_reversion(
                sym, hold_ticks=hold, threshold_bps=thresh
            )
            if len(pnl) < 20:
                continue

            mean_pnl = pnl.mean()
            std_pnl = pnl.std() if pnl.std() > 0 else 1
            win = (pnl > 0).mean()

            # IC from deviations
            if ic_pairs:
                devs, fwds = zip(*ic_pairs)
                ic_val, _ = spearmanr(devs, fwds)
            else:
                ic_val = 0

            # Approximate daily sharpe
            trades_per_day = n_trades / 12  # ~12 trading days
            daily_pnl = mean_pnl * trades_per_day
            daily_std = std_pnl * np.sqrt(trades_per_day)
            sharpe_d = daily_pnl / daily_std * np.sqrt(252) if daily_std > 0 else 0

            if mean_pnl > 0:
                best_configs.append((sym, thresh, hold, mean_pnl, std_pnl, win, sharpe_d, len(pnl), ic_val))

            # Only print interesting combos
            if thresh in [5.0, 10.0] and hold in [50, 100]:
                print(f"{sym:8s} | {thresh:6.1f} | {hold:5d} | {mean_pnl:9.2f} | {std_pnl:8.2f} | "
                      f"{win:5.1%} | {sharpe_d:8.2f} | {len(pnl):8d} | {ic_val:8.4f}")

print("\n\n=== BEST POSITIVE CONFIGURATIONS ===")
if best_configs:
    best_configs.sort(key=lambda x: -x[3])
    for sym, thresh, hold, mean, std, win, sharpe, n, ic in best_configs[:15]:
        print(f"  {sym} thresh={thresh:.0f}bps hold={hold}: mean={mean:.2f}bps, "
              f"win={win:.1%}, sharpe={sharpe:.2f}, n={n}, ic={ic:.4f}")
else:
    print("  No positive configurations found.")


# === VWAP reversion with mid-price returns (no spread crossing) ===
print("\n\n=== VWAP DEVIATION IC (mid-price, no execution) ===")
for sym in STOCKS[:6]:
    dates = sorted(f.stem for f in (DATA / sym).glob("*.parquet"))
    all_pairs = []

    for date_str in dates:
        df = pd.read_parquet(DATA / sym / f"{date_str}.parquet")
        ticks = df[df["type"] == "Tick"].sort_values("exch_ts").reset_index(drop=True)
        if len(ticks) < 500:
            continue

        prices = ticks["price_scaled"].values.astype(float)
        volumes = ticks["volume"].values.astype(float)

        cum_pv = np.cumsum(prices * volumes)
        cum_v = np.cumsum(volumes)
        vwap = cum_pv / np.maximum(cum_v, 1)

        for h in [50, 100, 200]:
            for i in range(200, len(prices) - h, h):
                dev = (prices[i] - vwap[i]) / vwap[i] * 10000
                fwd = (prices[i + h] - prices[i]) / prices[i] * 10000
                if np.isfinite(dev) and np.isfinite(fwd):
                    all_pairs.append((dev, fwd, h))

    if all_pairs:
        for target_h in [50, 100, 200]:
            pairs_h = [(d, f) for d, f, h in all_pairs if h == target_h]
            if len(pairs_h) < 50:
                continue
            devs, fwds = zip(*pairs_h)
            devs = np.array(devs)
            fwds = np.array(fwds)
            ic, _ = spearmanr(devs, fwds)
            # Expected mid-price P&L of reversal trade
            # When dev > 0, we short; when dev < 0, we long
            rev_pnl = -np.sign(devs) * fwds
            print(f"  {sym} h={target_h:3d}: IC={ic:.4f}, rev_pnl_mean={rev_pnl.mean():.2f}bps, "
                  f"rev_pnl_std={rev_pnl.std():.2f}bps, n={len(pairs_h)}")
