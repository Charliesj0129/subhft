"""R29 D4 × Top Alphas — Fixed Backtest

Fixes from review:
1. Exact session filter: 08:45-13:45 CST (00:45-05:45 UTC)
2. Conservative entry/exit: BUY at ask, SELL at bid (not mid)
3. No tick subsampling — feed every tick to alpha
4. Last 5 trading days as strict holdout (never seen in train)
5. Walk-forward: train on first N-5 days, test on remaining 5
"""

import math
import importlib
import subprocess
import numpy as np
import pandas as pd
from io import StringIO
from scipy import stats

# ── Config ────────────────────────────────────────────────────────────────

HOLD_BARS = 1800       # 30 min hold in 1s bars
SAMPLE_INTERVAL = 300  # signal every 5 min
OPEN_PERIOD_S = 1800   # 30 min opening
COST_COMM_TAX_PTS = 2.3  # commission (~1) + tax (~1.3)
# Spread cost is now modeled explicitly via ask-entry / bid-exit

# Front-month mapping
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

HOLDOUT_DAYS = 5  # last 5 days as strict out-of-sample

ALPHA_CONFIGS = {
    "critical_trend_reversion": {
        "module": "research.alphas.critical_trend_reversion.impl",
        "class": "CriticalTrendReversion",
        "input": "mid_x2",
    },
    "multiscale_trend_reversion": {
        "module": "research.alphas.multiscale_trend_reversion.impl",
        "class": "MultiscaleTrendReversion",
        "input": "mid_price",
    },
    "regime_adaptive_ofi": {
        "module": "research.alphas.regime_adaptive_ofi.impl",
        "class": "RegimeAdaptiveOFI",
        "input": "full",
    },
    "vr_momentum": {
        "module": "research.alphas.vr_momentum.impl",
        "class": "VRMomentum",
        "input": "mid_x2",
    },
    "intraday_jump_recovery": {
        "module": "research.alphas.intraday_jump_recovery.impl",
        "class": "IntradayJumpRecovery",
        "input": "mid_x2",
    },
    "log_gofi": {
        "module": "research.alphas.log_gofi.impl",
        "class": "LogGofiAlpha",
        "input": "bidask",
    },
}

SCALE = 1_000_000  # 1 index point = 1e6 in stored value


def query_ck(sql: str) -> str:
    result = subprocess.run(
        ["docker", "exec", "clickhouse", "clickhouse-client",
         "--query", sql, "--format", "TabSeparatedWithNames"],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"CK error: {result.stderr[:500]}")
    return result.stdout


def load_day_from_ck(date_str: str, symbol: str) -> pd.DataFrame:
    """Load one day's BidAsk L1 from CK, filtered to exact regular session."""
    sql = f"""
    SELECT
        exch_ts,
        bids_price[1] as best_bid_px,
        asks_price[1] as best_ask_px,
        bids_vol[1] as best_bid_qty,
        asks_vol[1] as best_ask_qty
    FROM hft.market_data
    WHERE symbol = '{symbol}'
      AND type = 'BidAsk'
      AND toDate(toDateTime64(exch_ts/1e9, 3)) = '{date_str}'
      AND bids_price[1] > 0
      AND asks_price[1] > 0
      AND asks_price[1] > bids_price[1]
      AND toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei') >= '{date_str} 08:45:00'
      AND toDateTime64(exch_ts/1e9, 3, 'Asia/Taipei') <= '{date_str} 13:45:00'
    ORDER BY exch_ts
    """
    raw = query_ck(sql)
    if not raw.strip():
        return pd.DataFrame()

    df = pd.read_csv(StringIO(raw), sep="\t")
    if len(df) < 100:
        return pd.DataFrame()

    df["mid"] = (df["best_bid_px"] + df["best_ask_px"]) / 2.0
    df["spread"] = df["best_ask_px"] - df["best_bid_px"]
    df["ts_s"] = df["exch_ts"].astype(float) / 1e9

    return df.reset_index(drop=True)


def compute_ofi(bid_px, ask_px, bid_qty, ask_qty) -> np.ndarray:
    """Vectorized OFI from arrays."""
    n = len(bid_px)
    ofi = np.zeros(n)
    for i in range(1, n):
        if bid_px[i] > bid_px[i - 1]:
            bd = bid_qty[i]
        elif bid_px[i] == bid_px[i - 1]:
            bd = bid_qty[i] - bid_qty[i - 1]
        else:
            bd = -bid_qty[i - 1]

        if ask_px[i] < ask_px[i - 1]:
            ad = ask_qty[i]
        elif ask_px[i] == ask_px[i - 1]:
            ad = ask_qty[i] - ask_qty[i - 1]
        else:
            ad = -ask_qty[i - 1]

        ofi[i] = bd - ad
    return ofi


def make_alpha(cfg: dict):
    """Create alpha instance from config."""
    mod = importlib.import_module(cfg["module"])
    cls = getattr(mod, cfg["class"])
    return cls()


def feed_alpha(alpha, cfg: dict, bp, ap, bq, aq, mid, spread_bps):
    """Feed one tick to alpha, return signal."""
    inp = cfg["input"]
    if inp == "mid_x2":
        r = alpha.update(int(bp + ap))
    elif inp == "mid_price":
        r = alpha.update(float(mid))
    elif inp == "full":
        r = alpha.update(bp, ap, bq, aq, mid, spread_bps)
    elif inp == "bidask":
        r = alpha.update(bid_px=bp, ask_px=ap, bid_qty=bq, ask_qty=aq)
    else:
        return 0
    return r.get("signal", 0) if isinstance(r, dict) else (r or 0)


def run_day(alpha_name: str, cfg: dict, ba: pd.DataFrame) -> dict | None:
    """Run alpha on one day with conservative execution model."""
    alpha = make_alpha(cfg)
    alpha.reset()

    ts_s = ba["ts_s"].values
    bp = ba["best_bid_px"].values
    ap = ba["best_ask_px"].values
    bq = ba["best_bid_qty"].values
    aq = ba["best_ask_qty"].values
    mids = ba["mid"].values
    spreads = ba["spread"].values
    n = len(ba)

    start_ts = ts_s[0]
    open_end = start_ts + OPEN_PERIOD_S

    # ── Opening features (from first 30 min) ──
    ofi = compute_ofi(bp, ap, bq, aq)
    open_mask = ts_s < open_end
    n_open = open_mask.sum()
    if n_open < 100:
        return None

    open_ofi_abs = abs(ofi[open_mask].sum())
    open_mid_s = mids[0]
    open_mid_e = mids[n_open - 1]
    open_abs_ret = abs((open_mid_e - open_mid_s) / open_mid_s) if open_mid_s > 0 else 0

    # Opening volatility from 5-min bars
    open_ts_floor = np.floor(ts_s[:n_open]).astype(np.int64)
    unique_5min = np.unique(open_ts_floor // 300)
    if len(unique_5min) >= 3:
        bar_mids = []
        for b5 in unique_5min:
            mask_5 = (open_ts_floor // 300) == b5
            if mask_5.any():
                bar_mids.append(mids[:n_open][mask_5][-1])
        bar_mids = np.array(bar_mids)
        open_vol = np.std(np.diff(bar_mids) / bar_mids[:-1]) if len(bar_mids) >= 3 else 0.0
    else:
        open_vol = 0.0

    # ── Feed ALL ticks to alpha (no subsampling) ──
    # Collect signal at each second
    ts_floor_all = np.floor(ts_s).astype(np.int64)
    signals = {}  # ts_1s -> last signal value
    bids_at = {}  # ts_1s -> last bid price
    asks_at = {}  # ts_1s -> last ask price

    for i in range(n):
        mid_v = mids[i]
        sp_bps = spreads[i] / mid_v * 10000 if mid_v > 0 else 0
        try:
            sig = feed_alpha(alpha, cfg, bp[i], ap[i], bq[i], aq[i], mid_v, sp_bps)
        except Exception:
            sig = 0

        t1s = ts_floor_all[i]
        signals[t1s] = sig
        bids_at[t1s] = bp[i]
        asks_at[t1s] = ap[i]

    # ── Generate trades with conservative execution ──
    rest_start = int(open_end)
    rest_end = int(ts_s[-1]) - HOLD_BARS
    trades = []
    trade_details = []

    t = rest_start
    while t <= rest_end:
        # Find latest nonzero signal at or near t
        sig_val = 0
        for dt in range(0, min(SAMPLE_INTERVAL, 60)):
            s = signals.get(t - dt, 0)
            if s != 0:
                sig_val = s
                break

        if sig_val != 0:
            direction = 1 if sig_val > 0 else -1

            # Conservative execution:
            # BUY: enter at ask (worst price), exit at bid
            # SELL: enter at bid (worst price), exit at ask
            if direction == 1:
                entry_price = asks_at.get(t)  # buy at ask
                exit_price = None
                for dt2 in range(-30, 31):
                    p = bids_at.get(t + HOLD_BARS + dt2)
                    if p is not None:
                        exit_price = p  # sell at bid
                        break
            else:
                entry_price = bids_at.get(t)  # sell at bid
                exit_price = None
                for dt2 in range(-30, 31):
                    p = asks_at.get(t + HOLD_BARS + dt2)
                    if p is not None:
                        exit_price = p  # buy back at ask
                        break

            if entry_price and exit_price and entry_price > 0:
                # P&L: for long, profit = exit_bid - entry_ask
                # for short, profit = entry_bid - exit_ask
                raw_pnl = direction * (exit_price - entry_price) / SCALE
                net_pnl = raw_pnl - COST_COMM_TAX_PTS  # subtract commission+tax
                trades.append(net_pnl)
                trade_details.append({
                    "ts": t, "dir": direction,
                    "entry": entry_price / SCALE, "exit": exit_price / SCALE,
                    "raw_pnl": raw_pnl, "net_pnl": net_pnl,
                })

        t += SAMPLE_INTERVAL

    if not trades:
        return None

    trades_arr = np.array(trades)
    return {
        "open_ofi_abs": open_ofi_abs,
        "open_abs_ret": open_abs_ret,
        "open_volatility": open_vol,
        "n_trades": len(trades_arr),
        "mean_pnl": trades_arr.mean(),
        "total_pnl": trades_arr.sum(),
        "win_rate": (trades_arr > 0).sum() / len(trades_arr),
        "std_pnl": trades_arr.std(),
        "trades": trades_arr,
    }


def sharpe(trades: np.ndarray, per_day: float = 10.0) -> float:
    if len(trades) < 2 or np.std(trades) < 1e-10:
        return 0.0
    return np.mean(trades) / np.std(trades) * math.sqrt(252 * per_day)


def main():
    # ── Load all days ──
    print("Loading from ClickHouse (exact regular session 08:45-13:45 CST)...")
    all_dates = sorted(FRONT_MONTH.keys())
    day_data = {}

    for date_str in all_dates:
        symbol = FRONT_MONTH[date_str]
        print(f"  {date_str} ({symbol})...", end=" ", flush=True)
        try:
            ba = load_day_from_ck(date_str, symbol)
            if len(ba) >= 5000:
                day_data[date_str] = ba
                spread_med = ba["spread"].median() / SCALE
                print(f"{len(ba)} events, med_spread={spread_med:.1f} pts")
            else:
                print(f"SKIP ({len(ba)})")
        except Exception as e:
            print(f"ERROR: {str(e)[:80]}")

    dates_sorted = sorted(day_data.keys())
    n_days = len(dates_sorted)
    print(f"\nLoaded {n_days} days")

    if n_days < HOLDOUT_DAYS + 5:
        print("Not enough days!")
        return

    train_dates = dates_sorted[:-HOLDOUT_DAYS]
    holdout_dates = dates_sorted[-HOLDOUT_DAYS:]
    print(f"Train: {len(train_dates)} days ({train_dates[0]} to {train_dates[-1]})")
    print(f"Holdout: {len(holdout_dates)} days ({holdout_dates[0]} to {holdout_dates[-1]})")

    # ── Run each alpha ──
    for alpha_name, cfg in ALPHA_CONFIGS.items():
        print(f"\n{'='*75}")
        print(f"ALPHA: {alpha_name}")
        print(f"  Execution: BUY@ask, SELL@bid + {COST_COMM_TAX_PTS} pts comm+tax")
        print(f"{'='*75}")

        train_sessions = []
        holdout_sessions = []

        for date_str in dates_sorted:
            ba = day_data[date_str]
            try:
                result = run_day(alpha_name, cfg, ba)
            except Exception as e:
                print(f"  {date_str}: ERROR - {str(e)[:60]}")
                continue

            if result is None or result["n_trades"] < 3:
                print(f"  {date_str}: no trades")
                continue

            result["day"] = date_str
            is_holdout = date_str in holdout_dates
            tag = "  [HOLDOUT]" if is_holdout else ""

            print(f"  {date_str}: {result['n_trades']} trades, "
                  f"mean={result['mean_pnl']:+.1f} pts, "
                  f"wr={result['win_rate']:.0%}{tag}")

            if is_holdout:
                holdout_sessions.append(result)
            else:
                train_sessions.append(result)

        if len(train_sessions) < 5:
            print(f"  SKIP — only {len(train_sessions)} train sessions")
            continue

        # ── Train set analysis ──
        train_trades = np.concatenate([s["trades"] for s in train_sessions])
        base_mean = train_trades.mean()
        base_wr = (train_trades > 0).sum() / len(train_trades)
        base_sharpe = sharpe(train_trades)

        print(f"\n  TRAIN BASE ({len(train_sessions)} days, {len(train_trades)} trades):")
        print(f"    mean={base_mean:+.2f} pts, wr={base_wr:.1%}, sharpe={base_sharpe:.2f}")

        # Regime filter on train set
        sdf = pd.DataFrame([{k: v for k, v in s.items() if k != "trades"} for s in train_sessions])

        best_feat = ""
        best_which = ""
        best_impr = -999
        best_train_sharpe = base_sharpe

        for feat in ["open_ofi_abs", "open_abs_ret", "open_volatility"]:
            med = sdf[feat].median()
            high_idx = sdf[feat] >= med
            low_idx = ~high_idx

            ht = np.concatenate([train_sessions[i]["trades"] for i in range(len(train_sessions)) if high_idx.iloc[i]])
            lt = np.concatenate([train_sessions[i]["trades"] for i in range(len(train_sessions)) if low_idx.iloc[i]])

            if len(ht) < 10 or len(lt) < 10:
                continue

            hs, ls = sharpe(ht), sharpe(lt)
            which = "HIGH" if hs >= ls else "LOW"
            impr = max(hs, ls) - base_sharpe

            print(f"    {feat}: HIGH mean={ht.mean():+.1f} sharpe={hs:.2f} ({len(ht)}) | "
                  f"LOW mean={lt.mean():+.1f} sharpe={ls:.2f} ({len(lt)}) | best={which} Δ={impr:+.2f}")

            if impr > best_impr:
                best_impr = impr
                best_feat = feat
                best_which = which
                best_train_sharpe = max(hs, ls)

        # ── LOO on train set ──
        if best_feat:
            loo_filt = []
            loo_all = []
            for i in range(len(train_sessions)):
                others = [train_sessions[j] for j in range(len(train_sessions)) if j != i]
                test = train_sessions[i]

                threshold = np.median([s[best_feat] for s in others])
                high_pnl = np.mean([s["mean_pnl"] for s in others if s[best_feat] >= threshold] or [0])
                low_pnl = np.mean([s["mean_pnl"] for s in others if s[best_feat] < threshold] or [0])
                trade_high = high_pnl >= low_pnl

                if (trade_high and test[best_feat] >= threshold) or \
                   (not trade_high and test[best_feat] < threshold):
                    loo_filt.extend(test["trades"].tolist())
                loo_all.extend(test["trades"].tolist())

            loo_all_arr = np.array(loo_all)
            loo_filt_arr = np.array(loo_filt) if loo_filt else np.array([0.0])

            print(f"\n  TRAIN LOO (filter={best_feat}:{best_which}):")
            print(f"    base: {sharpe(loo_all_arr):.2f} sharpe, mean={loo_all_arr.mean():+.1f}")
            print(f"    filt: {sharpe(loo_filt_arr):.2f} sharpe, mean={loo_filt_arr.mean():+.1f}, n={len(loo_filt_arr)}")

        # ── HOLDOUT (strict out-of-sample) ──
        if holdout_sessions:
            holdout_trades = np.concatenate([s["trades"] for s in holdout_sessions])
            h_mean = holdout_trades.mean()
            h_wr = (holdout_trades > 0).sum() / len(holdout_trades)
            h_sharpe = sharpe(holdout_trades)

            print(f"\n  HOLDOUT BASE ({len(holdout_sessions)} days, {len(holdout_trades)} trades):")
            print(f"    mean={h_mean:+.2f} pts, wr={h_wr:.1%}, sharpe={h_sharpe:.2f}")

            # Apply regime filter learned from train
            if best_feat:
                train_threshold = sdf[best_feat].median()
                train_high_pnl = np.mean([s["mean_pnl"] for s in train_sessions if s[best_feat] >= train_threshold] or [0])
                train_low_pnl = np.mean([s["mean_pnl"] for s in train_sessions if s[best_feat] < train_threshold] or [0])
                trade_high = train_high_pnl >= train_low_pnl

                h_filt_trades = []
                h_filt_days = 0
                for s in holdout_sessions:
                    if (trade_high and s[best_feat] >= train_threshold) or \
                       (not trade_high and s[best_feat] < train_threshold):
                        h_filt_trades.extend(s["trades"].tolist())
                        h_filt_days += 1

                if h_filt_trades:
                    hf = np.array(h_filt_trades)
                    print(f"\n  HOLDOUT FILTERED (filter={best_feat}:{best_which}, "
                          f"threshold from train={train_threshold:.1f}):")
                    print(f"    {h_filt_days}/{len(holdout_sessions)} days passed filter")
                    print(f"    mean={hf.mean():+.2f} pts, wr={(hf>0).sum()/len(hf):.1%}, "
                          f"sharpe={sharpe(hf):.2f}, n={len(hf)} trades")
                    print(f"    total_pnl={hf.sum():+.1f} pts over {h_filt_days} days")
                else:
                    print(f"\n  HOLDOUT FILTERED: 0 days passed filter — no trades")
        else:
            print(f"\n  NO HOLDOUT SESSIONS")

        # ── Verdict ──
        print(f"\n  {'─'*50}")
        if holdout_sessions and best_feat:
            if h_filt_trades:
                hf_mean = np.array(h_filt_trades).mean()
                verdict = "PASS" if hf_mean > 0 else "FAIL"
                print(f"  VERDICT: {verdict} (holdout filtered mean={hf_mean:+.1f} pts)")
            else:
                print(f"  VERDICT: INCONCLUSIVE (filter rejected all holdout days)")
        else:
            h_mean_v = holdout_trades.mean() if holdout_sessions else 0
            verdict = "PASS" if h_mean_v > 0 else "FAIL"
            print(f"  VERDICT: {verdict} (holdout base mean={h_mean_v:+.1f} pts, no filter)")


if __name__ == "__main__":
    main()
