"""R29 Large Order Follow + D4 Regime Filter Test

Signal: When price moves >= THRESHOLD pts in 1 second (large order impact),
enter in the direction of the move (follow, not fade).
Hold for HOLD_BARS seconds, then exit.

Conservative execution: BUY@ask, SELL@bid + commission/tax.
D4 regime filter: split by opening features.
5-day holdout.
"""

import math
import subprocess
import numpy as np
import pandas as pd
from io import StringIO
from scipy import stats

SCALE = 1_000_000
COST_COMM_TAX = 2.3  # pts
HOLD_SECS = [60, 300, 900, 1800]  # 1min, 5min, 15min, 30min
JUMP_THRESHOLDS = [10, 15, 20]  # minimum 1s price move in points
OPEN_PERIOD_S = 1800
COOLDOWN_S = 60  # min seconds between trades
HOLDOUT_DAYS = 5

FRONT_MONTH = {
    "2026-01-27": "TXFB6", "2026-01-28": "TXFB6",
    "2026-01-29": "TXFB6", "2026-01-30": "TXFB6", "2026-01-31": "TXFB6",
    "2026-02-04": "TXFB6", "2026-02-05": "TXFB6",
    "2026-02-23": "TXFD6", "2026-02-24": "TXFD6", "2026-02-25": "TXFD6",
    "2026-02-26": "TXFC6",
    "2026-03-03": "TXFC6", "2026-03-04": "TXFC6", "2026-03-05": "TXFC6",
    "2026-03-06": "TXFC6", "2026-03-09": "TXFC6", "2026-03-10": "TXFC6",
    "2026-03-11": "TXFC6", "2026-03-12": "TXFC6", "2026-03-13": "TXFC6",
    "2026-03-16": "TXFC6", "2026-03-17": "TXFC6",
    "2026-03-18": "TXFE6",
    "2026-03-20": "TXFD6", "2026-03-23": "TXFD6",
    "2026-03-24": "TXFD6", "2026-03-27": "TXFD6",
}


def query_ck(sql: str) -> str:
    result = subprocess.run(
        ["docker", "exec", "clickhouse", "clickhouse-client",
         "--query", sql, "--format", "TabSeparatedWithNames"],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:500])
    return result.stdout


def load_1s_bars(date_str: str, symbol: str) -> pd.DataFrame:
    """Load 1-second OHLC bars from CK, regular session only."""
    sql = f"""
    SELECT
        toUnixTimestamp(toDateTime64(exch_ts/1e9, 0)) as ts_1s,
        argMin((bids_price[1] + asks_price[1]) / 2, exch_ts) as open_mid,
        argMax((bids_price[1] + asks_price[1]) / 2, exch_ts) as close_mid,
        argMax(bids_price[1], exch_ts) as close_bid,
        argMax(asks_price[1], exch_ts) as close_ask,
        argMin(bids_price[1], exch_ts) as open_bid,
        argMin(asks_price[1], exch_ts) as open_ask,
        count() as n_ticks
    FROM hft.market_data
    WHERE symbol = '{symbol}' AND type = 'BidAsk'
        AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date_str}'
        AND bids_price[1] > 0 AND asks_price[1] > bids_price[1]
        AND toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei') >= '{date_str} 08:45:00'
        AND toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei') <= '{date_str} 13:45:00'
    GROUP BY ts_1s
    ORDER BY ts_1s
    """
    raw = query_ck(sql)
    if not raw.strip():
        return pd.DataFrame()
    df = pd.read_csv(StringIO(raw), sep="\t")
    return df


def compute_ofi_from_bars(df: pd.DataFrame) -> np.ndarray:
    """Approximate OFI from 1s bar close prices (simplified)."""
    mids = df["close_mid"].values
    n = len(mids)
    ofi = np.zeros(n)
    for i in range(1, n):
        ofi[i] = mids[i] - mids[i - 1]  # directional proxy
    return ofi


def run_day(df: pd.DataFrame, jump_thresh: int, hold_s: int) -> dict | None:
    """Run large-order-follow strategy on one day's 1s bars."""
    if len(df) < 1000:
        return None

    ts = df["ts_1s"].values
    open_mid = df["open_mid"].values
    close_mid = df["close_mid"].values
    close_bid = df["close_bid"].values
    close_ask = df["close_ask"].values

    start_ts = ts[0]
    open_end = start_ts + OPEN_PERIOD_S

    # Opening features
    open_mask = ts < open_end
    n_open = open_mask.sum()
    if n_open < 30:
        return None

    ofi_proxy = compute_ofi_from_bars(df)
    open_ofi_abs = abs(ofi_proxy[open_mask].sum()) / SCALE
    open_mid_s = close_mid[0]
    open_mid_e = close_mid[n_open - 1]
    open_abs_ret = abs((open_mid_e - open_mid_s) / open_mid_s) if open_mid_s > 0 else 0

    # 5-min opening volatility
    unique_5min = np.unique((ts[:n_open] - start_ts) // 300)
    bar_mids = []
    for b5 in unique_5min:
        mask_5 = ((ts[:n_open] - start_ts) // 300) == b5
        if mask_5.any():
            bar_mids.append(close_mid[:n_open][mask_5][-1])
    bar_mids_arr = np.array(bar_mids)
    open_vol = np.std(np.diff(bar_mids_arr) / bar_mids_arr[:-1]) if len(bar_mids_arr) >= 3 else 0.0

    # Build lookup maps
    ts_to_idx = {t: i for i, t in enumerate(ts)}
    thresh_raw = jump_thresh * SCALE

    # Detect jumps and generate trades
    trades = []
    last_trade_ts = 0

    for i in range(1, len(df)):
        if ts[i] < open_end:
            continue  # skip opening period

        delta = close_mid[i] - open_mid[i]  # intra-second price move

        if abs(delta) < thresh_raw:
            continue

        if ts[i] - last_trade_ts < COOLDOWN_S:
            continue

        direction = 1 if delta > 0 else -1

        # Conservative entry: BUY at current ask, SELL at current bid
        if direction == 1:
            entry_price = close_ask[i]
        else:
            entry_price = close_bid[i]

        # Exit at ts + hold_s
        exit_ts = ts[i] + hold_s
        exit_idx = ts_to_idx.get(exit_ts)
        if exit_idx is None:
            # Find closest
            for dt in range(-30, 31):
                exit_idx = ts_to_idx.get(exit_ts + dt)
                if exit_idx is not None:
                    break
        if exit_idx is None:
            continue

        # Conservative exit: LONG exits at bid, SHORT exits at ask
        if direction == 1:
            exit_price = close_bid[exit_idx]
        else:
            exit_price = close_ask[exit_idx]

        if entry_price <= 0 or exit_price <= 0:
            continue

        raw_pnl = direction * (exit_price - entry_price) / SCALE
        net_pnl = raw_pnl - COST_COMM_TAX
        trades.append(net_pnl)
        last_trade_ts = ts[i]

    if not trades:
        return None

    t = np.array(trades)
    return {
        "open_ofi_abs": open_ofi_abs,
        "open_abs_ret": open_abs_ret,
        "open_volatility": open_vol,
        "n_trades": len(t),
        "mean_pnl": t.mean(),
        "total_pnl": t.sum(),
        "win_rate": (t > 0).sum() / len(t),
        "trades": t,
    }


def sharpe(t: np.ndarray, per_day: float = 5.0) -> float:
    if len(t) < 2 or np.std(t) < 1e-10:
        return 0.0
    return np.mean(t) / np.std(t) * math.sqrt(252 * per_day)


def main():
    print("Loading 1s bars from ClickHouse...")
    dates_sorted = sorted(FRONT_MONTH.keys())
    day_bars = {}

    for d in dates_sorted:
        sym = FRONT_MONTH[d]
        print(f"  {d} ({sym})...", end=" ", flush=True)
        try:
            df = load_1s_bars(d, sym)
            if len(df) >= 1000:
                spread_med = (df["close_ask"] - df["close_bid"]).median() / SCALE
                day_bars[d] = df
                print(f"{len(df)} bars, spread={spread_med:.1f} pts")
            else:
                print(f"SKIP ({len(df)})")
        except Exception as e:
            print(f"ERROR: {str(e)[:60]}")

    dates_avail = sorted(day_bars.keys())
    n_days = len(dates_avail)
    train_dates = dates_avail[:-HOLDOUT_DAYS]
    holdout_dates = dates_avail[-HOLDOUT_DAYS:]
    print(f"\n{n_days} days loaded. Train={len(train_dates)}, Holdout={len(holdout_dates)}")

    # Grid search: threshold × hold period
    print(f"\n{'='*90}")
    print(f"GRID: jump_threshold × hold_period (conservative execution)")
    print(f"{'='*90}")

    grid_results = []

    for thresh in JUMP_THRESHOLDS:
        for hold in HOLD_SECS:
            label = f"jump>={thresh}pts, hold={hold//60}min"
            train_sessions = []
            holdout_sessions = []

            for d in dates_avail:
                r = run_day(day_bars[d], thresh, hold)
                if r and r["n_trades"] >= 2:
                    r["day"] = d
                    if d in holdout_dates:
                        holdout_sessions.append(r)
                    else:
                        train_sessions.append(r)

            if len(train_sessions) < 5:
                print(f"  {label}: <5 train days, SKIP")
                continue

            train_t = np.concatenate([s["trades"] for s in train_sessions])
            tmean = train_t.mean()
            twr = (train_t > 0).sum() / len(train_t)
            ts_ = sharpe(train_t)

            # Holdout
            if holdout_sessions:
                hold_t = np.concatenate([s["trades"] for s in holdout_sessions])
                hmean = hold_t.mean()
                hwr = (hold_t > 0).sum() / len(hold_t)
                hs = sharpe(hold_t)
            else:
                hmean, hwr, hs, hold_t = 0, 0, 0, np.array([])

            print(f"  {label}: "
                  f"train {len(train_t)} trades mean={tmean:+.1f} wr={twr:.0%} S={ts_:.1f} | "
                  f"holdout {len(hold_t)} trades mean={hmean:+.1f} wr={hwr:.0%} S={hs:.1f}")

            # D4 regime filter on train
            best_feat = ""
            best_which = ""
            best_impr = -999

            sdf = pd.DataFrame([{k: v for k, v in s.items() if k != "trades"} for s in train_sessions])
            for feat in ["open_ofi_abs", "open_abs_ret", "open_volatility"]:
                med = sdf[feat].median()
                hi = sdf[feat] >= med
                lo = ~hi
                ht = np.concatenate([train_sessions[i]["trades"] for i in range(len(train_sessions)) if hi.iloc[i]])
                lt = np.concatenate([train_sessions[i]["trades"] for i in range(len(train_sessions)) if lo.iloc[i]])
                if len(ht) < 5 or len(lt) < 5:
                    continue
                hs_h, hs_l = sharpe(ht), sharpe(lt)
                which = "HIGH" if hs_h >= hs_l else "LOW"
                impr = max(hs_h, hs_l) - ts_
                if impr > best_impr:
                    best_impr = impr
                    best_feat = feat
                    best_which = which

            # Apply D4 to holdout
            d4_holdout_mean = 0.0
            d4_holdout_n = 0
            if best_feat and holdout_sessions:
                threshold = sdf[best_feat].median()
                train_high = np.mean([s["mean_pnl"] for s in train_sessions if s[best_feat] >= threshold] or [0])
                train_low = np.mean([s["mean_pnl"] for s in train_sessions if s[best_feat] < threshold] or [0])
                trade_high = train_high >= train_low

                filt_trades = []
                for s in holdout_sessions:
                    if (trade_high and s[best_feat] >= threshold) or \
                       (not trade_high and s[best_feat] < threshold):
                        filt_trades.extend(s["trades"].tolist())

                if filt_trades:
                    fa = np.array(filt_trades)
                    d4_holdout_mean = fa.mean()
                    d4_holdout_n = len(fa)
                    print(f"    D4({best_feat}:{best_which}): "
                          f"holdout filt mean={d4_holdout_mean:+.1f} n={d4_holdout_n}")

            grid_results.append({
                "thresh": thresh, "hold": hold,
                "train_mean": tmean, "train_sharpe": ts_, "train_n": len(train_t),
                "train_wr": twr,
                "holdout_mean": hmean, "holdout_sharpe": hs, "holdout_n": len(hold_t),
                "holdout_wr": hwr,
                "d4_feat": f"{best_feat}:{best_which}" if best_feat else "",
                "d4_holdout_mean": d4_holdout_mean, "d4_holdout_n": d4_holdout_n,
            })

    # Summary table
    print(f"\n{'='*100}")
    print(f"SUMMARY TABLE")
    print(f"{'='*100}")
    print(f"{'Thresh':>6s} {'Hold':>5s} | {'TrainN':>6s} {'TrMean':>7s} {'TrWR':>5s} {'TrS':>5s} | "
          f"{'HoldN':>5s} {'HoMean':>7s} {'HoWR':>5s} {'HoS':>5s} | "
          f"{'D4 HoMn':>7s} {'D4 N':>5s} {'Filter':<20s}")
    print("-" * 100)

    for r in grid_results:
        print(f"{r['thresh']:>5d}pt {r['hold']//60:>4d}m | "
              f"{r['train_n']:>6d} {r['train_mean']:>+7.1f} {r['train_wr']:>5.0%} {r['train_sharpe']:>5.1f} | "
              f"{r['holdout_n']:>5d} {r['holdout_mean']:>+7.1f} {r['holdout_wr']:>5.0%} {r['holdout_sharpe']:>5.1f} | "
              f"{r['d4_holdout_mean']:>+7.1f} {r['d4_holdout_n']:>5d} {r['d4_feat']:<20s}")

    # Best combo
    best = max(grid_results, key=lambda x: x["holdout_mean"])
    print(f"\n  BEST HOLDOUT: jump>={best['thresh']}pts hold={best['hold']//60}min "
          f"holdout_mean={best['holdout_mean']:+.1f} pts")

    if best["d4_holdout_n"] > 0:
        print(f"  BEST D4 HOLDOUT: {best['d4_feat']} "
              f"mean={best['d4_holdout_mean']:+.1f} pts (n={best['d4_holdout_n']})")

    # Verdict
    winner = best["holdout_mean"] > 0 or (best["d4_holdout_mean"] > 0 and best["d4_holdout_n"] > 5)
    print(f"\n  VERDICT: {'PROMISING' if winner else 'FAIL'}")


if __name__ == "__main__":
    main()
