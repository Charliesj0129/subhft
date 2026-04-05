"""
R28 Stage 3: TX→TMF Lead-Lag at Longer Horizons — Deep Validation

Analyses:
1. Reproduce with correct dvol + bid/ask entry (mid vs realistic)
2. Day-by-day stability
3. SL/TP simulation with realistic entry/exit
4. Exit price reality (slippage at exit)
5. Statistical rigor (t-test, bootstrap CI, Cohen's d)
6. Signal timing characteristics
7. Regime split (volatile vs calm days)
8. Extended horizons (1hr, 2hr, 4hr)
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
SCALE = 1_000_000  # price_scaled / SCALE = points
RT_COST_PTS = 7.4  # round-trip cost in points (4 pts fees + 3.4 pts spread)
FEE_PTS = 4.0      # RT fees only
SIGNAL_DELAY_NS = 37_000_000   # 37 ms broker RTT for entry
EXIT_DELAY_NS = 47_000_000     # 47 ms for exit order fill

DVOL_THRESHOLDS = [10, 20, 50]
HORIZONS_MIN = [1, 2, 5, 10, 15, 30]
EXTENDED_HORIZONS_MIN = [60, 120, 240]

SL_LEVELS = [10, 15, 20, 30]
TRAILING_TP = [(8, 5), (10, 8), (15, 10), (20, 15)]  # (trail, activation)
TIME_KILLS_MIN = [10, 15, 30]


def ck(sql: str) -> str:
    r = subprocess.run(
        ["docker", "exec", "clickhouse", "clickhouse-client", "--query", sql],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if r.returncode != 0:
        print(f"CK ERROR: {r.stderr[:500]}", file=sys.stderr)
        return ""
    return r.stdout.strip()


def parse_tsv(raw: str, dtypes: list) -> list[tuple]:
    """Parse tab-separated CK output into list of tuples with given dtypes."""
    rows = []
    for line in raw.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        row = tuple(dt(p) for dt, p in zip(dtypes, parts))
        rows.append(row)
    return rows


def ns_to_date(ns: int) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ns / 1e9, tz=datetime.timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Data Loading
# ---------------------------------------------------------------------------
def load_tx_ticks() -> np.ndarray:
    """Load TX ticks with dvol computation. Returns structured array."""
    print("Loading TX ticks...")
    raw = ck("""
        SELECT exch_ts, price_scaled, volume,
               volume - lagInFrame(volume, 1, 0)
                 OVER (PARTITION BY toDate(exch_ts/1e9) ORDER BY exch_ts) as dvol
        FROM hft.market_data
        WHERE symbol='TXFD6' AND type='Tick'
          AND toDate(exch_ts/1e9) >= '2026-03-19'
        ORDER BY exch_ts
    """)
    rows = parse_tsv(raw, [int, int, int, int])
    if not rows:
        print("ERROR: No TX tick data!", file=sys.stderr)
        sys.exit(1)
    dt = np.dtype([("ts", "i8"), ("price", "i8"), ("vol", "i8"), ("dvol", "i8")])
    arr = np.array(rows, dtype=dt)
    # Fix day boundaries: dvol < 0 means new day, use raw volume
    mask = arr["dvol"] < 0
    arr["dvol"][mask] = arr["vol"][mask]
    # First tick of dataset: dvol = vol (lagInFrame returns 0)
    # Already handled by CK lag default=0
    print(f"  Loaded {len(arr)} TX ticks, date range: {ns_to_date(arr['ts'][0])} to {ns_to_date(arr['ts'][-1])}")
    return arr


def load_tmf_bidask() -> np.ndarray:
    """Load TMF BidAsk data. Returns structured array."""
    print("Loading TMF BidAsk...")
    raw = ck("""
        SELECT exch_ts, bids_price[1], asks_price[1]
        FROM hft.market_data
        WHERE symbol='TMFD6' AND type='BidAsk'
          AND toDate(exch_ts/1e9) >= '2026-03-19'
        ORDER BY exch_ts
    """)
    rows = parse_tsv(raw, [int, int, int])
    dt = np.dtype([("ts", "i8"), ("bid", "i8"), ("ask", "i8")])
    arr = np.array(rows, dtype=dt)
    # Filter out zero bids/asks (market open artifacts)
    valid = (arr["bid"] > 0) & (arr["ask"] > 0)
    arr = arr[valid]
    print(f"  Loaded {len(arr)} TMF BidAsk events")
    return arr


def load_tmf_ticks() -> np.ndarray:
    """Load TMF tick data for SL/TP simulation."""
    print("Loading TMF Ticks...")
    raw = ck("""
        SELECT exch_ts, price_scaled
        FROM hft.market_data
        WHERE symbol='TMFD6' AND type='Tick'
          AND toDate(exch_ts/1e9) >= '2026-03-19'
        ORDER BY exch_ts
    """)
    rows = parse_tsv(raw, [int, int])
    dt = np.dtype([("ts", "i8"), ("price", "i8")])
    arr = np.array(rows, dtype=dt)
    print(f"  Loaded {len(arr)} TMF ticks")
    return arr


# ---------------------------------------------------------------------------
# Signal Generation
# ---------------------------------------------------------------------------
def generate_signals(tx: np.ndarray, dvol_min: int) -> list[dict]:
    """Generate TX signals where dvol >= threshold and dp != 0."""
    signals = []
    for i in range(1, len(tx)):
        dv = tx["dvol"][i]
        if dv < dvol_min:
            continue
        dp = tx["price"][i] - tx["price"][i - 1]
        if dp == 0:
            continue
        signals.append({
            "ts": int(tx["ts"][i]),
            "tx_price_pts": tx["price"][i] / SCALE,
            "dp_pts": dp / SCALE,
            "dvol": int(dv),
            "direction": 1 if dp > 0 else -1,  # 1=buy TMF, -1=sell TMF
            "date": ns_to_date(int(tx["ts"][i])),
        })
    return signals


# ---------------------------------------------------------------------------
# TMF Price Lookup
# ---------------------------------------------------------------------------
def find_tmf_ba_at(tmf_ba: np.ndarray, target_ts: int) -> tuple[float, float, float]:
    """Find TMF bid/ask at or just before target_ts. Returns (bid_pts, ask_pts, mid_pts)."""
    idx = np.searchsorted(tmf_ba["ts"], target_ts, side="right") - 1
    if idx < 0:
        return (np.nan, np.nan, np.nan)
    bid = tmf_ba["bid"][idx] / SCALE
    ask = tmf_ba["ask"][idx] / SCALE
    mid = (bid + ask) / 2.0
    return (bid, ask, mid)


def find_tmf_ba_at_delayed(tmf_ba: np.ndarray, signal_ts: int, delay_ns: int) -> tuple[float, float, float]:
    """Find TMF bid/ask at signal_ts + delay (broker RTT)."""
    return find_tmf_ba_at(tmf_ba, signal_ts + delay_ns)


def find_tmf_tick_price_at(tmf_ticks: np.ndarray, target_ts: int) -> float:
    """Find TMF last trade price at or before target_ts."""
    idx = np.searchsorted(tmf_ticks["ts"], target_ts, side="right") - 1
    if idx < 0:
        return np.nan
    return tmf_ticks["price"][idx] / SCALE


# ---------------------------------------------------------------------------
# Analysis 1: Forward Returns with Mid vs Bid/Ask Entry
# ---------------------------------------------------------------------------
def analysis1(signals: list[dict], tmf_ba: np.ndarray, dvol_min: int):
    """Reproduce forward returns with correct dvol and bid/ask vs mid entry."""
    print(f"\n{'='*70}")
    print(f"ANALYSIS 1: Forward Returns (dvol >= {dvol_min}, N={len(signals)})")
    print(f"{'='*70}")

    all_horizons = HORIZONS_MIN + EXTENDED_HORIZONS_MIN
    results = {}

    for h_min in all_horizons:
        h_ns = h_min * 60 * 1_000_000_000
        mid_returns = []
        ba_returns = []

        for sig in signals:
            # Entry price at signal_ts + 37ms
            entry_bid, entry_ask, entry_mid = find_tmf_ba_at_delayed(
                tmf_ba, sig["ts"], SIGNAL_DELAY_NS
            )
            if np.isnan(entry_mid):
                continue

            # Realistic entry: buy at ask, sell at bid
            entry_ba = entry_ask if sig["direction"] == 1 else entry_bid

            # Exit price at signal_ts + horizon
            exit_bid, exit_ask, exit_mid = find_tmf_ba_at(
                tmf_ba, sig["ts"] + h_ns
            )
            if np.isnan(exit_mid):
                continue

            # Forward return in direction of signal
            ret_mid = (exit_mid - entry_mid) * sig["direction"]
            ret_ba = (exit_mid - entry_ba) * sig["direction"]  # mid exit for comparison
            mid_returns.append(ret_mid)
            ba_returns.append(ret_ba)

        mid_arr = np.array(mid_returns)
        ba_arr = np.array(ba_returns)
        if len(mid_arr) == 0:
            continue

        slippage = np.mean(mid_arr) - np.mean(ba_arr)

        results[h_min] = {
            "n": len(mid_arr),
            "mid_mean": float(np.mean(mid_arr)),
            "mid_median": float(np.median(mid_arr)),
            "mid_std": float(np.std(mid_arr)),
            "mid_dir_acc": float(np.mean(mid_arr > 0)),
            "mid_pct_gt_cost": float(np.mean(mid_arr > RT_COST_PTS)),
            "ba_mean": float(np.mean(ba_arr)),
            "ba_median": float(np.median(ba_arr)),
            "ba_dir_acc": float(np.mean(ba_arr > 0)),
            "ba_pct_gt_cost": float(np.mean(ba_arr > RT_COST_PTS)),
            "entry_slippage_mean": float(slippage),
        }

        print(f"\n  Horizon {h_min:>4}min | N={len(mid_arr)}")
        print(f"    Mid entry:    mean={np.mean(mid_arr):+.2f}  median={np.median(mid_arr):+.2f}  "
              f"dir_acc={np.mean(mid_arr > 0):.1%}  >cost={np.mean(mid_arr > RT_COST_PTS):.1%}")
        print(f"    B/A entry:    mean={np.mean(ba_arr):+.2f}  median={np.median(ba_arr):+.2f}  "
              f"dir_acc={np.mean(ba_arr > 0):.1%}  >cost={np.mean(ba_arr > RT_COST_PTS):.1%}")
        print(f"    Slippage:     {slippage:+.2f} pts (mid - ba)")

    return results


# ---------------------------------------------------------------------------
# Analysis 2: Day-by-Day Stability
# ---------------------------------------------------------------------------
def analysis2(signals: list[dict], tmf_ba: np.ndarray, horizon_min: int = 15):
    """Day-by-day breakdown of returns at a given horizon."""
    print(f"\n{'='*70}")
    print(f"ANALYSIS 2: Day-by-Day Stability (horizon={horizon_min}min)")
    print(f"{'='*70}")

    h_ns = horizon_min * 60 * 1_000_000_000
    day_data: dict[str, list[float]] = {}

    for sig in signals:
        entry_bid, entry_ask, entry_mid = find_tmf_ba_at_delayed(
            tmf_ba, sig["ts"], SIGNAL_DELAY_NS
        )
        if np.isnan(entry_mid):
            continue
        entry_ba = entry_ask if sig["direction"] == 1 else entry_bid

        exit_bid, exit_ask, exit_mid = find_tmf_ba_at(tmf_ba, sig["ts"] + h_ns)
        if np.isnan(exit_mid):
            continue

        ret = (exit_mid - entry_ba) * sig["direction"]
        day_data.setdefault(sig["date"], []).append(ret)

    results = {}
    positive_days = 0
    for day in sorted(day_data.keys()):
        rets = np.array(day_data[day])
        mean_r = float(np.mean(rets))
        med_r = float(np.median(rets))
        dir_acc = float(np.mean(rets > 0))
        net_mean = mean_r - FEE_PTS  # net of fees
        if net_mean > 0:
            positive_days += 1
        results[day] = {
            "n": len(rets),
            "mean": mean_r,
            "median": med_r,
            "dir_acc": dir_acc,
            "net_mean_after_fees": net_mean,
        }
        print(f"  {day}: N={len(rets):>3}  mean={mean_r:+7.2f}  median={med_r:+7.2f}  "
              f"dir_acc={dir_acc:.1%}  net={net_mean:+7.2f}")

    total_days = len(results)
    print(f"\n  Positive days (net > 0): {positive_days}/{total_days}")
    results["_summary"] = {"positive_days": positive_days, "total_days": total_days}
    return results


# ---------------------------------------------------------------------------
# Analysis 3: SL/TP Simulation
# ---------------------------------------------------------------------------
def analysis3(signals: list[dict], tmf_ba: np.ndarray, tmf_ticks: np.ndarray):
    """SL/TP simulation with realistic bid/ask entry and exit."""
    print(f"\n{'='*70}")
    print("ANALYSIS 3: SL/TP Simulation (bid/ask entry, realistic exit)")
    print(f"{'='*70}")

    results = {}

    for sl_pts in SL_LEVELS:
        for trail_pts, activation_pts in TRAILING_TP:
            for time_kill_min in TIME_KILLS_MIN:
                key = f"SL{sl_pts}_Trail{trail_pts}@{activation_pts}_TK{time_kill_min}m"
                pnls = _simulate_sltp(
                    signals, tmf_ba, tmf_ticks,
                    sl_pts, trail_pts, activation_pts, time_kill_min,
                )
                if not pnls:
                    continue
                pnl_arr = np.array(pnls)
                wins = pnl_arr[pnl_arr > 0]
                losses = pnl_arr[pnl_arr <= 0]

                # Max consecutive losses
                max_consec = _max_consecutive_losses(pnl_arr)

                stats = {
                    "n_trades": len(pnl_arr),
                    "win_rate": float(np.mean(pnl_arr > 0)),
                    "avg_win": float(np.mean(wins)) if len(wins) > 0 else 0.0,
                    "avg_loss": float(np.mean(losses)) if len(losses) > 0 else 0.0,
                    "expectation": float(np.mean(pnl_arr)),
                    "total_pnl": float(np.sum(pnl_arr)),
                    "max_consec_losses": max_consec,
                    "std": float(np.std(pnl_arr)),
                    "sharpe_per_trade": float(np.mean(pnl_arr) / np.std(pnl_arr)) if np.std(pnl_arr) > 0 else 0.0,
                }
                results[key] = stats

    # Print top 10 by expectation
    sorted_keys = sorted(results.keys(), key=lambda k: results[k]["expectation"], reverse=True)
    print(f"\n  Top 10 configurations by expectation per trade (after {FEE_PTS} pts fees):")
    print(f"  {'Config':<35} {'N':>4} {'WR':>6} {'E[PnL]':>8} {'AvgW':>7} {'AvgL':>7} {'MaxCL':>5} {'Sharpe':>7}")
    for k in sorted_keys[:10]:
        s = results[k]
        print(f"  {k:<35} {s['n_trades']:>4} {s['win_rate']:>5.1%} {s['expectation']:>+7.2f} "
              f"{s['avg_win']:>+6.1f} {s['avg_loss']:>+6.1f} {s['max_consec_losses']:>5} {s['sharpe_per_trade']:>+6.3f}")

    return results


def _simulate_sltp(
    signals: list[dict],
    tmf_ba: np.ndarray,
    tmf_ticks: np.ndarray,
    sl_pts: float,
    trail_pts: float,
    activation_pts: float,
    time_kill_min: int,
) -> list[float]:
    """Simulate SL/TP for all signals. Returns list of net PnL per trade."""
    time_kill_ns = time_kill_min * 60 * 1_000_000_000
    pnls = []

    for sig in signals:
        entry_bid, entry_ask, entry_mid = find_tmf_ba_at_delayed(
            tmf_ba, sig["ts"], SIGNAL_DELAY_NS
        )
        if np.isnan(entry_mid):
            continue

        direction = sig["direction"]
        entry_price = entry_ask if direction == 1 else entry_bid

        # Find TMF BidAsk events from entry to entry + time_kill
        entry_ts = sig["ts"] + SIGNAL_DELAY_NS
        end_ts = sig["ts"] + time_kill_ns

        # Slice TMF BidAsk for this window
        start_idx = np.searchsorted(tmf_ba["ts"], entry_ts, side="left")
        end_idx = np.searchsorted(tmf_ba["ts"], end_ts, side="right")

        if start_idx >= len(tmf_ba):
            continue

        window = tmf_ba[start_idx:end_idx]
        if len(window) == 0:
            continue

        # Track best unrealized profit for trailing stop
        best_profit = 0.0
        trailing_active = False
        exit_price = None
        exit_reason = None

        for j in range(len(window)):
            ts_j = window["ts"][j]
            bid_j = window["bid"][j] / SCALE
            ask_j = window["ask"][j] / SCALE
            mid_j = (bid_j + ask_j) / 2.0

            # Current unrealized PnL (using mid for monitoring, exit uses bid/ask)
            if direction == 1:
                unrealized = mid_j - entry_price
            else:
                unrealized = entry_price - mid_j

            # Update best profit
            if unrealized > best_profit:
                best_profit = unrealized

            # Check SL (using mid for trigger, exit at bid/ask + delay)
            if unrealized <= -sl_pts:
                # SL triggered — exit at delayed bid/ask
                exit_bid, exit_ask, _ = find_tmf_ba_at(tmf_ba, ts_j + EXIT_DELAY_NS)
                if not np.isnan(exit_bid):
                    exit_price = exit_bid if direction == 1 else exit_ask
                else:
                    exit_price = bid_j if direction == 1 else ask_j
                exit_reason = "SL"
                break

            # Check trailing TP
            if best_profit >= activation_pts:
                trailing_active = True

            if trailing_active and (best_profit - unrealized) >= trail_pts:
                # Trailing stop triggered
                exit_bid, exit_ask, _ = find_tmf_ba_at(tmf_ba, ts_j + EXIT_DELAY_NS)
                if not np.isnan(exit_bid):
                    exit_price = exit_bid if direction == 1 else exit_ask
                else:
                    exit_price = bid_j if direction == 1 else ask_j
                exit_reason = "TP"
                break

        # Time kill exit
        if exit_price is None:
            # Exit at end of window using bid/ask
            if len(window) > 0:
                last_bid = window["bid"][-1] / SCALE
                last_ask = window["ask"][-1] / SCALE
                exit_price = last_bid if direction == 1 else last_ask
                exit_reason = "TK"
            else:
                continue

        # Calculate PnL
        if direction == 1:
            gross_pnl = exit_price - entry_price
        else:
            gross_pnl = entry_price - exit_price

        net_pnl = gross_pnl - FEE_PTS
        pnls.append(net_pnl)

    return pnls


def _max_consecutive_losses(pnls: np.ndarray) -> int:
    max_cl = 0
    current = 0
    for p in pnls:
        if p <= 0:
            current += 1
            max_cl = max(max_cl, current)
        else:
            current = 0
    return max_cl


# ---------------------------------------------------------------------------
# Analysis 4: Exit Slippage
# ---------------------------------------------------------------------------
def analysis4(signals: list[dict], tmf_ba: np.ndarray):
    """Measure exit slippage: price at trigger vs price at trigger + 47ms."""
    print(f"\n{'='*70}")
    print("ANALYSIS 4: Exit Price Reality (slippage at exit)")
    print(f"{'='*70}")

    # For each signal, compare exit mid at 15min vs exit bid/ask at 15min+47ms
    h_ns = 15 * 60 * 1_000_000_000
    slippages = []

    for sig in signals:
        trigger_ts = sig["ts"] + h_ns
        # Price at trigger time
        _, _, trigger_mid = find_tmf_ba_at(tmf_ba, trigger_ts)
        if np.isnan(trigger_mid):
            continue

        # Price at trigger + exit delay
        exit_bid, exit_ask, exit_mid = find_tmf_ba_at(tmf_ba, trigger_ts + EXIT_DELAY_NS)
        if np.isnan(exit_bid):
            continue

        direction = sig["direction"]
        # Exit price for closing: long closes at bid, short closes at ask
        exit_price = exit_bid if direction == 1 else exit_ask

        # Slippage = what we lose from delay
        # Positive = we lose money
        if direction == 1:
            slip = trigger_mid - exit_price  # ideal mid vs actual bid
        else:
            slip = exit_price - trigger_mid  # actual ask vs ideal mid

        slippages.append(slip)

    slip_arr = np.array(slippages)
    results = {
        "n": len(slip_arr),
        "mean_slippage": float(np.mean(slip_arr)),
        "median_slippage": float(np.median(slip_arr)),
        "p95_slippage": float(np.percentile(slip_arr, 95)),
        "max_slippage": float(np.max(slip_arr)),
        "pct_adverse": float(np.mean(slip_arr > 0)),
    }
    print(f"  N={len(slip_arr)}")
    print(f"  Mean exit slippage:   {np.mean(slip_arr):+.2f} pts")
    print(f"  Median exit slippage: {np.median(slip_arr):+.2f} pts")
    print(f"  P95 exit slippage:    {np.percentile(slip_arr, 95):+.2f} pts")
    print(f"  Max exit slippage:    {np.max(slip_arr):+.2f} pts")
    print(f"  % adverse (>0):       {np.mean(slip_arr > 0):.1%}")
    return results


# ---------------------------------------------------------------------------
# Analysis 5: Statistical Rigor
# ---------------------------------------------------------------------------
def analysis5(signals: list[dict], tmf_ba: np.ndarray, horizon_min: int = 15):
    """t-test, bootstrap CI, Cohen's d, day-clustered bootstrap."""
    print(f"\n{'='*70}")
    print(f"ANALYSIS 5: Statistical Rigor (horizon={horizon_min}min)")
    print(f"{'='*70}")

    from scipy import stats as sp_stats

    h_ns = horizon_min * 60 * 1_000_000_000
    net_returns = []
    day_returns: dict[str, list[float]] = {}

    for sig in signals:
        entry_bid, entry_ask, _ = find_tmf_ba_at_delayed(tmf_ba, sig["ts"], SIGNAL_DELAY_NS)
        if np.isnan(entry_bid):
            continue
        entry_ba = entry_ask if sig["direction"] == 1 else entry_bid

        exit_bid, exit_ask, exit_mid = find_tmf_ba_at(tmf_ba, sig["ts"] + h_ns)
        if np.isnan(exit_mid):
            continue

        # Exit at bid/ask (closing)
        exit_price = exit_bid if sig["direction"] == 1 else exit_ask
        gross = (exit_price - entry_ba) * sig["direction"]
        net = gross - FEE_PTS
        net_returns.append(net)
        day_returns.setdefault(sig["date"], []).append(net)

    net_arr = np.array(net_returns)
    n = len(net_arr)
    mean_net = np.mean(net_arr)
    std_net = np.std(net_arr, ddof=1)

    # t-test: H0: mean <= 0
    t_stat, p_value = sp_stats.ttest_1samp(net_arr, 0)
    p_one_sided = p_value / 2 if t_stat > 0 else 1 - p_value / 2

    # Cohen's d
    cohens_d = mean_net / std_net if std_net > 0 else 0.0

    # Bootstrap 95% CI (standard)
    rng = np.random.default_rng(42)
    n_boot = 10_000
    boot_means = np.array([np.mean(rng.choice(net_arr, size=n, replace=True)) for _ in range(n_boot)])
    ci_low, ci_high = np.percentile(boot_means, [2.5, 97.5])

    # Day-clustered bootstrap
    days = sorted(day_returns.keys())
    day_arrays = [np.array(day_returns[d]) for d in days]
    n_days = len(days)
    cluster_boot_means = []
    for _ in range(n_boot):
        sampled_days = rng.choice(n_days, size=n_days, replace=True)
        combined = np.concatenate([day_arrays[d] for d in sampled_days])
        cluster_boot_means.append(np.mean(combined))
    cluster_boot_means = np.array(cluster_boot_means)
    cluster_ci_low, cluster_ci_high = np.percentile(cluster_boot_means, [2.5, 97.5])

    results = {
        "n": n,
        "mean_net_return": float(mean_net),
        "std": float(std_net),
        "t_stat": float(t_stat),
        "p_one_sided": float(p_one_sided),
        "cohens_d": float(cohens_d),
        "bootstrap_ci_95": [float(ci_low), float(ci_high)],
        "day_clustered_ci_95": [float(cluster_ci_low), float(cluster_ci_high)],
        "ci_excludes_zero": bool(ci_low > 0),
        "day_cluster_ci_excludes_zero": bool(cluster_ci_low > 0),
    }

    print(f"  N={n}, Mean net return (after fees): {mean_net:+.2f} pts")
    print(f"  Std: {std_net:.2f} pts")
    print(f"  t-stat: {t_stat:.3f}, p-value (one-sided): {p_one_sided:.4f}")
    print(f"  Cohen's d: {cohens_d:.4f}")
    print(f"  Bootstrap 95% CI: [{ci_low:+.2f}, {ci_high:+.2f}]")
    print(f"  Day-clustered 95% CI: [{cluster_ci_low:+.2f}, {cluster_ci_high:+.2f}]")
    sig_str = "YES" if ci_low > 0 and cluster_ci_low > 0 else "NO"
    print(f"  ** Statistically significant (both CIs > 0): {sig_str} **")
    return results


# ---------------------------------------------------------------------------
# Analysis 6: Signal Timing Characteristics
# ---------------------------------------------------------------------------
def analysis6(signals: list[dict]):
    """Inter-signal intervals, concurrency, time-of-day distribution."""
    print(f"\n{'='*70}")
    print("ANALYSIS 6: Signal Timing Characteristics")
    print(f"{'='*70}")

    if not signals:
        return {}

    # Inter-signal intervals (within same day)
    day_signals: dict[str, list[int]] = {}
    for sig in signals:
        day_signals.setdefault(sig["date"], []).append(sig["ts"])

    intervals_sec = []
    for day, tss in day_signals.items():
        tss_sorted = sorted(tss)
        for i in range(1, len(tss_sorted)):
            interval = (tss_sorted[i] - tss_sorted[i - 1]) / 1e9
            intervals_sec.append(interval)

    int_arr = np.array(intervals_sec) if intervals_sec else np.array([0.0])

    # Max concurrent signals (assuming 15min holding)
    hold_ns = 15 * 60 * 1_000_000_000
    all_ts = sorted([s["ts"] for s in signals])
    max_concurrent = 0
    for i, ts in enumerate(all_ts):
        concurrent = sum(1 for t in all_ts if ts - hold_ns <= t <= ts)
        max_concurrent = max(max_concurrent, concurrent)

    # Time of day distribution (hour buckets, UTC+8 for TWSE)
    import datetime
    hour_counts: dict[int, int] = {}
    for sig in signals:
        dt = datetime.datetime.fromtimestamp(sig["ts"] / 1e9, tz=datetime.timezone(datetime.timedelta(hours=8)))
        h = dt.hour
        hour_counts[h] = hour_counts.get(h, 0) + 1

    results = {
        "n_signals": len(signals),
        "n_days": len(day_signals),
        "signals_per_day": {d: len(ts) for d, ts in sorted(day_signals.items())},
        "inter_signal_interval_sec": {
            "mean": float(np.mean(int_arr)),
            "median": float(np.median(int_arr)),
            "min": float(np.min(int_arr)),
            "p10": float(np.percentile(int_arr, 10)),
            "p25": float(np.percentile(int_arr, 25)),
        },
        "max_concurrent_15min": max_concurrent,
        "hour_distribution_tw": {str(h): c for h, c in sorted(hour_counts.items())},
    }

    print(f"  Total signals: {len(signals)} across {len(day_signals)} days")
    print(f"  Signals per day:")
    for d, ts in sorted(day_signals.items()):
        print(f"    {d}: {len(ts)}")
    print(f"  Inter-signal interval (sec):")
    print(f"    mean={np.mean(int_arr):.1f}  median={np.median(int_arr):.1f}  "
          f"min={np.min(int_arr):.1f}  P10={np.percentile(int_arr, 10):.1f}  P25={np.percentile(int_arr, 25):.1f}")
    print(f"  Max concurrent signals (15min hold): {max_concurrent}")
    print(f"  Hour distribution (TW time): {dict(sorted(hour_counts.items()))}")
    return results


# ---------------------------------------------------------------------------
# Analysis 7: Regime Split
# ---------------------------------------------------------------------------
def analysis7(signals: list[dict], tmf_ba: np.ndarray, horizon_min: int = 15):
    """Compare volatile vs calm days within March."""
    print(f"\n{'='*70}")
    print(f"ANALYSIS 7: Regime Split (horizon={horizon_min}min)")
    print(f"{'='*70}")

    volatile_days = {"2026-03-20", "2026-03-23", "2026-03-24"}
    h_ns = horizon_min * 60 * 1_000_000_000

    regimes = {"volatile": [], "calm": []}
    for sig in signals:
        entry_bid, entry_ask, _ = find_tmf_ba_at_delayed(tmf_ba, sig["ts"], SIGNAL_DELAY_NS)
        if np.isnan(entry_bid):
            continue
        entry_ba = entry_ask if sig["direction"] == 1 else entry_bid
        exit_bid, exit_ask, _ = find_tmf_ba_at(tmf_ba, sig["ts"] + h_ns)
        if np.isnan(exit_bid):
            continue
        exit_price = exit_bid if sig["direction"] == 1 else exit_ask
        net = (exit_price - entry_ba) * sig["direction"] - FEE_PTS

        if sig["date"] in volatile_days:
            regimes["volatile"].append(net)
        else:
            regimes["calm"].append(net)

    results = {}
    for regime, rets in regimes.items():
        arr = np.array(rets)
        if len(arr) == 0:
            results[regime] = {"n": 0}
            continue
        results[regime] = {
            "n": len(arr),
            "mean": float(np.mean(arr)),
            "median": float(np.median(arr)),
            "dir_acc": float(np.mean(arr > 0)),
            "total_pnl": float(np.sum(arr)),
        }
        print(f"  {regime:>10}: N={len(arr):>3}  mean={np.mean(arr):+7.2f}  "
              f"median={np.median(arr):+7.2f}  win_rate={np.mean(arr > 0):.1%}  "
              f"total={np.sum(arr):+.1f}")

    return results


# ---------------------------------------------------------------------------
# Analysis 8: Extended Horizons (already in Analysis 1)
# ---------------------------------------------------------------------------
def analysis8_reversion_check(signals: list[dict], tmf_ba: np.ndarray):
    """Check if signal continues to grow or reverts at 1hr+."""
    print(f"\n{'='*70}")
    print("ANALYSIS 8: Extended Horizons — Reversion Check")
    print(f"{'='*70}")

    all_h = [1, 2, 5, 10, 15, 30, 60, 120, 240]
    means = {}

    for h_min in all_h:
        h_ns = h_min * 60 * 1_000_000_000
        rets = []
        for sig in signals:
            entry_bid, entry_ask, _ = find_tmf_ba_at_delayed(tmf_ba, sig["ts"], SIGNAL_DELAY_NS)
            if np.isnan(entry_bid):
                continue
            entry_ba = entry_ask if sig["direction"] == 1 else entry_bid
            _, _, exit_mid = find_tmf_ba_at(tmf_ba, sig["ts"] + h_ns)
            if np.isnan(exit_mid):
                continue
            ret = (exit_mid - entry_ba) * sig["direction"]
            rets.append(ret)

        arr = np.array(rets)
        if len(arr) == 0:
            continue
        means[h_min] = {"n": len(arr), "mean": float(np.mean(arr)), "median": float(np.median(arr))}
        print(f"  {h_min:>4}min: N={len(arr):>3}  mean={np.mean(arr):+7.2f}  median={np.median(arr):+7.2f}")

    # Check reversion
    sorted_h = sorted(means.keys())
    if len(sorted_h) >= 3:
        peak_h = max(sorted_h, key=lambda h: means[h]["mean"])
        last_h = sorted_h[-1]
        if means[last_h]["mean"] < means[peak_h]["mean"] * 0.7:
            print(f"\n  ** REVERSION DETECTED: Peak at {peak_h}min ({means[peak_h]['mean']:+.2f}), "
                  f"drops to {means[last_h]['mean']:+.2f} at {last_h}min **")
        else:
            print(f"\n  ** PERSISTENT: Signal grows from {peak_h}min onward. Informed flow likely. **")

    return means


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("R28 Stage 3: TX→TMF Lead-Lag at Longer Horizons — Deep Validation")
    print("=" * 70)

    # Load data
    tx = load_tx_ticks()
    tmf_ba = load_tmf_bidask()
    tmf_ticks = load_tmf_ticks()

    all_results = {}

    for dvol_min in DVOL_THRESHOLDS:
        print(f"\n\n{'#'*70}")
        print(f"# DVOL THRESHOLD >= {dvol_min}")
        print(f"{'#'*70}")

        signals = generate_signals(tx, dvol_min)
        print(f"Generated {len(signals)} signals (dvol >= {dvol_min})")

        if len(signals) < 10:
            print(f"  Too few signals, skipping detailed analysis")
            all_results[f"dvol{dvol_min}"] = {"n_signals": len(signals), "skipped": True}
            continue

        dv_key = f"dvol{dvol_min}"

        # Analysis 1: Forward returns
        a1 = analysis1(signals, tmf_ba, dvol_min)

        # Analysis 2: Day-by-day
        a2 = analysis2(signals, tmf_ba, horizon_min=15)

        # Analysis 5: Statistical rigor (before SL/TP since it's simpler)
        a5 = analysis5(signals, tmf_ba, horizon_min=15)

        # Analysis 6: Timing
        a6 = analysis6(signals)

        # Analysis 7: Regime
        a7 = analysis7(signals, tmf_ba, horizon_min=15)

        # Analysis 8: Extended horizons
        a8 = analysis8_reversion_check(signals, tmf_ba)

        # Analysis 3: SL/TP (only for dvol >= 20 to save time)
        a3 = {}
        if dvol_min == 20:
            a3 = analysis3(signals, tmf_ba, tmf_ticks)

        # Analysis 4: Exit slippage (only for dvol >= 20)
        a4 = {}
        if dvol_min == 20:
            a4 = analysis4(signals, tmf_ba)

        all_results[dv_key] = {
            "n_signals": len(signals),
            "analysis1_forward_returns": a1,
            "analysis2_day_stability": a2,
            "analysis3_sltp": a3,
            "analysis4_exit_slippage": a4,
            "analysis5_stat_rigor": a5,
            "analysis6_timing": a6,
            "analysis7_regime": a7,
            "analysis8_extended": a8,
        }

    # Final verdict
    print(f"\n\n{'='*70}")
    print("FINAL VERDICT")
    print(f"{'='*70}")

    dv20 = all_results.get("dvol20", {})
    a5_res = dv20.get("analysis5_stat_rigor", {})
    a2_res = dv20.get("analysis2_day_stability", {})
    a3_res = dv20.get("analysis3_sltp", {})

    if a5_res:
        ci = a5_res.get("day_clustered_ci_95", [0, 0])
        print(f"  dvol>=20 at 15min:")
        print(f"    Net mean return (B/A entry+exit, after fees): {a5_res.get('mean_net_return', 0):+.2f} pts")
        print(f"    Day-clustered 95% CI: [{ci[0]:+.2f}, {ci[1]:+.2f}]")
        print(f"    p-value (one-sided): {a5_res.get('p_one_sided', 1):.4f}")
        print(f"    Cohen's d: {a5_res.get('cohens_d', 0):.4f}")

    if a2_res:
        summary = a2_res.get("_summary", {})
        print(f"    Positive days: {summary.get('positive_days', 0)}/{summary.get('total_days', 0)}")

    if a3_res:
        best_key = max(a3_res.keys(), key=lambda k: a3_res[k].get("expectation", -999)) if a3_res else None
        if best_key:
            best = a3_res[best_key]
            print(f"    Best SL/TP config: {best_key}")
            print(f"      Expectation: {best['expectation']:+.2f} pts/trade, WR: {best['win_rate']:.1%}")

    tradeable = (
        a5_res.get("day_cluster_ci_excludes_zero", False) and
        a5_res.get("p_one_sided", 1) < 0.05 and
        a2_res.get("_summary", {}).get("positive_days", 0) >= 5
    )
    print(f"\n  ** TRADEABLE: {'YES' if tradeable else 'NO'} **")

    # Save results
    out_path = Path("/home/charlie/hft_platform/outputs/team_artifacts/alpha-research-r28/stage3_results.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Convert numpy types for JSON serialization
    def convert(obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        return obj

    class NpEncoder(json.JSONEncoder):
        def default(self, obj):
            v = convert(obj)
            if v is not obj:
                return v
            return super().default(obj)

    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, cls=NpEncoder)
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
