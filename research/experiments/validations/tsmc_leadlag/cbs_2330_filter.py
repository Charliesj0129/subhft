"""
Direction B: CBS + 2330 Confirmation Filter

Tests whether TSMC 300s return can improve CBS entry quality.

Logic:
- CBS triggers when TMFD6 moves >=40 bps in 600s, enters contrarian
- 2330 300s return tells us TSMC momentum direction
- If TSMC already reversing (agrees with contrarian entry) = "Confirmed"
- If TSMC still trending with the move = "Denied"
- Compare PnL of confirmed vs denied CBS trades
"""

import numpy as np
import pandas as pd
from pathlib import Path
from scipy import stats

BASE = Path(__file__).resolve().parent.parent.parent.parent.parent
DATA_DIR = BASE / "research" / "data" / "processed" / "tsmc_leadlag"

CBS_THRESHOLD_BPS = 40  # 40 bps move triggers CBS
CBS_LOOKBACK_S = 600     # 600s window for detecting the move
CBS_HOLD_S = 300         # 300s hold period
CBS_STOPLOSS_BPS = 15    # 15 bps stop loss
TSMC_LB_S = 300          # 2330 lookback for confirmation signal
MIN_GAP_S = 600          # Minimum gap between CBS triggers (avoid clustering)


def load_all_days() -> dict:
    days = {}
    for f in sorted(DATA_DIR.glob("aligned_*.npz")):
        date_str = f.stem.replace("aligned_", "")
        data = np.load(f)
        stock = pd.DataFrame(data["stock"])
        futures = pd.DataFrame(data["futures"])
        stock["ts"] = pd.to_datetime(stock["local_ts"], unit="ns")
        futures["ts"] = pd.to_datetime(futures["local_ts"], unit="ns")
        stock = stock.set_index("ts").sort_index()
        futures = futures.set_index("ts").sort_index()
        s1 = stock.resample("1s").last().dropna(subset=["mid_price"])
        f1 = futures.resample("1s").last().dropna(subset=["mid_price"])
        common = s1.index.intersection(f1.index)
        if len(common) < 1000:
            continue
        df = pd.DataFrame(index=common)
        df["mid_stock"] = s1.loc[common, "mid_price"].values
        df["mid_fut"] = f1.loc[common, "mid_price"].values
        df["date"] = date_str
        days[date_str] = df
    return days


def find_cbs_triggers(mid_fut: np.ndarray, lookback: int, threshold_bps: float, min_gap: int):
    """Find CBS trigger points: where TMFD6 moved >= threshold_bps in lookback seconds."""
    n = len(mid_fut)
    triggers = []
    last_trigger = -min_gap

    for i in range(lookback, n - CBS_HOLD_S):
        if i - last_trigger < min_gap:
            continue
        past_price = mid_fut[i - lookback]
        curr_price = mid_fut[i]
        if past_price <= 0:
            continue
        move_bps = (curr_price - past_price) / past_price * 1e4

        if abs(move_bps) >= threshold_bps:
            # CBS enters contrarian: if market went UP, CBS goes SHORT (expects reversal)
            cbs_direction = -1 if move_bps > 0 else +1  # contrarian
            triggers.append({
                "idx": i,
                "move_bps": move_bps,
                "cbs_direction": cbs_direction,
            })
            last_trigger = i

    return triggers


def run():
    print("Loading aligned data...")
    days = load_all_days()
    print(f"Loaded {len(days)} days\n")

    all_trades = []

    for date_str, df in sorted(days.items()):
        mid_f = df["mid_fut"].values
        mid_s = df["mid_stock"].values

        triggers = find_cbs_triggers(mid_f, CBS_LOOKBACK_S, CBS_THRESHOLD_BPS, MIN_GAP_S)

        for t in triggers:
            idx = t["idx"]
            cbs_dir = t["cbs_direction"]

            # 2330 confirmation signal: 300s past return on TSMC
            if idx < TSMC_LB_S:
                continue
            stock_past = mid_s[idx - TSMC_LB_S]
            stock_now = mid_s[idx]
            if stock_past <= 0 or stock_now <= 0:
                continue
            tsmc_ret = np.log(stock_now / stock_past)
            tsmc_dir = np.sign(tsmc_ret)

            # Confirmation logic:
            # CBS is contrarian (cbs_dir is opposite to move direction)
            # "Confirmed" = TSMC already reversing = TSMC direction agrees with CBS direction
            # "Denied" = TSMC still trending = TSMC direction agrees with the original move
            confirmed = (tsmc_dir == cbs_dir) or (tsmc_dir == 0)

            # Forward return (CBS PnL): cbs_direction * log_return over hold period
            if idx + CBS_HOLD_S >= len(mid_f):
                continue
            entry_price = mid_f[idx]
            exit_price = mid_f[idx + CBS_HOLD_S]

            # Check stop loss along the path
            stopped = False
            stop_idx = idx + CBS_HOLD_S
            for j in range(idx + 1, idx + CBS_HOLD_S + 1):
                pnl_bps_j = cbs_dir * (mid_f[j] - entry_price) / entry_price * 1e4
                if pnl_bps_j < -CBS_STOPLOSS_BPS:
                    exit_price = mid_f[j]
                    stop_idx = j
                    stopped = True
                    break

            raw_pnl_bps = cbs_dir * (exit_price - entry_price) / entry_price * 1e4
            net_pnl_bps = raw_pnl_bps - 1.33  # RT cost

            all_trades.append({
                "date": date_str,
                "idx": idx,
                "move_bps": t["move_bps"],
                "cbs_dir": cbs_dir,
                "tsmc_ret": tsmc_ret,
                "tsmc_dir": tsmc_dir,
                "confirmed": confirmed,
                "raw_pnl_bps": raw_pnl_bps,
                "net_pnl_bps": net_pnl_bps,
                "stopped": stopped,
                "hold_s": stop_idx - idx,
            })

    trades_df = pd.DataFrame(all_trades)
    print(f"Total CBS triggers: {len(trades_df)}")
    print(f"  Confirmed (TSMC agrees with contrarian): {trades_df['confirmed'].sum()}")
    print(f"  Denied (TSMC still trending): {(~trades_df['confirmed']).sum()}")

    # ======================================================================
    # Analysis
    # ======================================================================
    print(f"\n{'=' * 70}")
    print("CBS TRADE ANALYSIS: ALL vs CONFIRMED vs DENIED")
    print(f"{'=' * 70}")

    for label, mask in [
        ("ALL", pd.Series(True, index=trades_df.index)),
        ("CONFIRMED", trades_df["confirmed"]),
        ("DENIED", ~trades_df["confirmed"]),
    ]:
        subset = trades_df[mask]
        if len(subset) == 0:
            print(f"\n{label}: no trades")
            continue

        n = len(subset)
        mean_raw = subset["raw_pnl_bps"].mean()
        mean_net = subset["net_pnl_bps"].mean()
        std = subset["net_pnl_bps"].std()
        win_rate = (subset["net_pnl_bps"] > 0).mean()
        median_pnl = subset["net_pnl_bps"].median()
        sharpe = mean_net / std * np.sqrt(252 * 15) if std > 0 else np.nan  # ~15 trades/day annualized
        stopped_pct = subset["stopped"].mean()

        # t-test vs 0
        t_stat, t_pval = stats.ttest_1samp(subset["net_pnl_bps"], 0)

        print(f"\n{label} (n={n}):")
        print(f"  Mean raw PnL:   {mean_raw:+.2f} bps")
        print(f"  Mean net PnL:   {mean_net:+.2f} bps (after 1.33 bps RT)")
        print(f"  Median net PnL: {median_pnl:+.2f} bps")
        print(f"  Std:            {std:.2f} bps")
        print(f"  Win rate:       {win_rate:.1%}")
        print(f"  Stop rate:      {stopped_pct:.1%}")
        print(f"  t-stat vs 0:    {t_stat:.2f} (p={t_pval:.4f})")
        print(f"  Ann. Sharpe:    {sharpe:.2f}")

    # ======================================================================
    # Confirmed vs Denied comparison
    # ======================================================================
    print(f"\n{'=' * 70}")
    print("CONFIRMED vs DENIED COMPARISON")
    print(f"{'=' * 70}")

    confirmed_trades = trades_df[trades_df["confirmed"]]
    denied_trades = trades_df[~trades_df["confirmed"]]

    if len(confirmed_trades) > 5 and len(denied_trades) > 5:
        diff_mean = confirmed_trades["net_pnl_bps"].mean() - denied_trades["net_pnl_bps"].mean()
        t_diff, p_diff = stats.ttest_ind(
            confirmed_trades["net_pnl_bps"], denied_trades["net_pnl_bps"]
        )
        print(f"  Confirmed mean: {confirmed_trades['net_pnl_bps'].mean():+.2f} bps")
        print(f"  Denied mean:    {denied_trades['net_pnl_bps'].mean():+.2f} bps")
        print(f"  Difference:     {diff_mean:+.2f} bps")
        print(f"  t-stat:         {t_diff:.2f} (p={p_diff:.4f})")
        print(f"  Confirmed WR:   {(confirmed_trades['net_pnl_bps'] > 0).mean():.1%}")
        print(f"  Denied WR:      {(denied_trades['net_pnl_bps'] > 0).mean():.1%}")

    # ======================================================================
    # Per-day summary
    # ======================================================================
    print(f"\n{'=' * 70}")
    print("PER-DAY SUMMARY")
    print(f"{'=' * 70}")

    print(f"{'Date':>12} {'N':>4} {'Conf':>5} {'Den':>4} {'All PnL':>8} {'Conf PnL':>9} {'Den PnL':>9}")
    print("-" * 55)

    for date_str in sorted(trades_df["date"].unique()):
        day = trades_df[trades_df["date"] == date_str]
        conf = day[day["confirmed"]]
        den = day[~day["confirmed"]]
        print(
            f"{date_str:>12} {len(day):>4} {len(conf):>5} {len(den):>4} "
            f"{day['net_pnl_bps'].mean():>+8.2f} "
            f"{conf['net_pnl_bps'].mean() if len(conf) > 0 else float('nan'):>+9.2f} "
            f"{den['net_pnl_bps'].mean() if len(den) > 0 else float('nan'):>+9.2f}"
        )

    # ======================================================================
    # Quintile split by TSMC signal strength
    # ======================================================================
    print(f"\n{'=' * 70}")
    print("CBS PnL BY TSMC SIGNAL STRENGTH (quintiles of |tsmc_ret|)")
    print(f"{'=' * 70}")

    trades_df["abs_tsmc"] = trades_df["tsmc_ret"].abs()
    if len(trades_df) >= 25:
        try:
            trades_df["tsmc_q"] = pd.qcut(trades_df["abs_tsmc"], 5, labels=False, duplicates="drop")
            for q in sorted(trades_df["tsmc_q"].unique()):
                qdf = trades_df[trades_df["tsmc_q"] == q]
                print(
                    f"  Q{int(q)+1} (|tsmc|): n={len(qdf)}, "
                    f"mean net={qdf['net_pnl_bps'].mean():+.2f} bps, "
                    f"WR={( qdf['net_pnl_bps'] > 0).mean():.1%}"
                )
        except ValueError:
            print("  Could not compute quintiles (insufficient unique values)")

    return trades_df


if __name__ == "__main__":
    trades = run()
