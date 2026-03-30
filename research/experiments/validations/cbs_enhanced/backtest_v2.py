"""
CBS Enhanced Backtest v2 — Direction B (refined)
==================================================
Key finding from v1: ATR-scaled stop effectively REMOVES the stop-loss,
producing +2.44 pts/trade OOS vs -2.76 baseline. The 15 bps stop-loss
is actively destroying value by cutting winners early.

This v2 run:
1. Stop-loss sweep: [no_stop, 15, 30, 50, 100, 200 bps]
2. phi_8min deeper exploration with properly calibrated thresholds
3. Spread gate (re-verified)
4. Combinations of best configs
5. Stopped-trade analysis: what happens to stopped trades if you DON'T stop?
"""

from __future__ import annotations

import math
import itertools
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy import stats as scipy_stats

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "raw" / "tmfd6"

ALL_DATES = [
    "2026-01-26", "2026-01-27", "2026-01-28", "2026-01-29", "2026-01-30",
    "2026-02-03", "2026-02-04", "2026-02-05", "2026-02-06", "2026-02-10",
    "2026-02-11", "2026-02-23", "2026-02-24", "2026-02-25",
    "2026-03-19", "2026-03-20", "2026-03-23", "2026-03-24", "2026-03-25",
    "2026-03-26",
]
IS_DATES = ALL_DATES[:14]
OOS_DATES = ALL_DATES[14:]

SESSION_START_SOD = 9 * 3600 + 15 * 60
SESSION_END_SOD = 13 * 3600 + 35 * 60
UTC_OFFSET = 8 * 3600

MOVE_THRESHOLD_BPS = 40
DETECT_WINDOW_NS = 600_000_000_000
HOLD_NS = 300_000_000_000
BASE_STOP_BPS = 15

RT_COST_PTS = 4.0
PT_VALUE_NTD = 10.0


@dataclass(slots=True)
class CBSTrade:
    entry_ts: int
    exit_ts: int
    entry_mid: float
    exit_mid: float
    direction: int
    move_bps: float
    exit_reason: str
    gross_pnl_pts: float
    net_pnl_pts: float
    spread_at_entry: float
    phi_8min_at_entry: float
    max_adverse_bps: float  # max adverse excursion during hold
    max_favorable_bps: float  # max favorable excursion during hold
    day: str


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_day(date_str: str) -> Optional[np.ndarray]:
    path = DATA_DIR / f"TMFD6_{date_str}_l1.npy"
    if not path.exists():
        return None
    data = np.load(str(path), allow_pickle=True)
    ts = data["local_ts"]
    sod = (ts / 1e9 + UTC_OFFSET) % 86400
    mask = (sod >= 8 * 3600 + 45 * 60) & (sod < 13 * 3600 + 45 * 60)
    filtered = data[mask]
    return filtered if len(filtered) > 0 else None


def load_all_days() -> dict[str, np.ndarray]:
    result = {}
    for d in ALL_DATES:
        arr = load_day(d)
        if arr is not None:
            result[d] = arr
    return result


# ---------------------------------------------------------------------------
# phi_8min computation
# ---------------------------------------------------------------------------
def compute_phi_8min(mid: np.ndarray, ts_ns: np.ndarray) -> np.ndarray:
    """EMA of tick-to-tick mid-price returns, halflife=8min."""
    n = len(mid)
    phi = np.zeros(n, dtype=np.float64)
    if n < 2:
        return phi

    halflife_ns = 480.0 * 1e9
    ema = 0.0
    for i in range(1, n):
        dt = float(ts_ns[i] - ts_ns[i - 1])
        if dt <= 0:
            phi[i] = ema
            continue
        ret = mid[i] - mid[i - 1]
        alpha = 1.0 - math.exp(-dt / halflife_ns)
        ema = alpha * ret + (1.0 - alpha) * ema
        phi[i] = ema
    return phi


def compute_phi_declining(phi: np.ndarray, ts_ns: np.ndarray,
                          lookback_ns: int = 30_000_000_000) -> np.ndarray:
    """True where |phi| has peaked and is declining over last 30s."""
    n = len(phi)
    result = np.zeros(n, dtype=np.bool_)
    abs_phi = np.abs(phi)
    for i in range(1, n):
        cutoff = ts_ns[i] - lookback_ns
        max_prev = 0.0
        j = i - 1
        while j >= 0 and ts_ns[j] >= cutoff:
            if abs_phi[j] > max_prev:
                max_prev = abs_phi[j]
            j -= 1
            if i - j > 5000:
                break
        result[i] = abs_phi[i] < max_prev * 0.90
    return result


# ---------------------------------------------------------------------------
# CBS Backtest Engine
# ---------------------------------------------------------------------------
def run_cbs_backtest(
    data: np.ndarray,
    day_label: str,
    stop_bps: float = BASE_STOP_BPS,
    no_stop: bool = False,
    phi_mode: str = "none",
    phi_threshold: float = 0.5,
    min_spread_pts: float = 0.0,
) -> list[CBSTrade]:
    """Run CBS with configurable stop and filters."""
    mid = data["mid_price"].astype(np.float64)
    bid = data["bid_px"].astype(np.float64)
    ask = data["ask_px"].astype(np.float64)
    ts = data["local_ts"].astype(np.int64)
    n = len(data)

    if n < 100:
        return []

    phi = compute_phi_8min(mid, ts)
    if phi_mode == "declining":
        phi_decl = compute_phi_declining(phi, ts)
    else:
        phi_decl = np.zeros(n, dtype=np.bool_)

    trades: list[CBSTrade] = []
    state = "idle"
    entry_ts = 0
    entry_mid = 0.0
    direction = 0
    next_allowed_ts = 0
    entry_spread = 0.0
    entry_phi = 0.0
    entry_move_bps = 0.0

    # For MAE/MFE tracking
    max_adverse = 0.0
    max_favorable = 0.0

    # Price buffer
    buf_ts = np.zeros(16384, dtype=np.int64)
    buf_mid = np.zeros(16384, dtype=np.float64)
    buf_start = 0
    buf_end = 0

    for i in range(n):
        now_ns = int(ts[i])
        mid_i = float(mid[i])
        bid_i = float(bid[i])
        ask_i = float(ask[i])
        spread_i = ask_i - bid_i

        if mid_i <= 0 or bid_i <= 0 or ask_i <= 0:
            continue

        # Update buffer
        cutoff = now_ns - DETECT_WINDOW_NS
        while buf_start < buf_end and buf_ts[buf_start % 16384] < cutoff:
            buf_start += 1
        idx = buf_end % 16384
        buf_ts[idx] = now_ns
        buf_mid[idx] = mid_i
        buf_end += 1

        sod = ((now_ns // 1_000_000_000) + UTC_OFFSET) % 86400
        in_session = SESSION_START_SOD <= sod <= SESSION_END_SOD

        if state == "positioned":
            elapsed = now_ns - entry_ts
            pnl_pts = direction * (mid_i - entry_mid)
            pnl_bps = pnl_pts / entry_mid * 10000.0 if entry_mid > 0 else 0.0

            # Track MAE/MFE
            if pnl_bps < 0 and abs(pnl_bps) > max_adverse:
                max_adverse = abs(pnl_bps)
            if pnl_bps > 0 and pnl_bps > max_favorable:
                max_favorable = pnl_bps

            exit_reason: Optional[str] = None

            if not no_stop and pnl_bps < -stop_bps:
                exit_reason = "stop_loss"

            if elapsed >= HOLD_NS:
                exit_reason = "time_exit"

            if exit_reason is not None:
                gross = direction * (mid_i - entry_mid)
                net = gross - RT_COST_PTS

                trades.append(CBSTrade(
                    entry_ts=entry_ts, exit_ts=now_ns,
                    entry_mid=entry_mid, exit_mid=mid_i,
                    direction=direction, move_bps=entry_move_bps,
                    exit_reason=exit_reason,
                    gross_pnl_pts=gross, net_pnl_pts=net,
                    spread_at_entry=entry_spread,
                    phi_8min_at_entry=entry_phi,
                    max_adverse_bps=max_adverse,
                    max_favorable_bps=max_favorable,
                    day=day_label,
                ))
                state = "idle"
                next_allowed_ts = entry_ts + HOLD_NS
                direction = 0
            continue

        # Idle: check entry
        if now_ns < next_allowed_ts:
            continue
        if not in_session:
            continue
        if buf_end - buf_start < 2:
            continue

        oldest_mid = buf_mid[buf_start % 16384]
        if oldest_mid <= 0:
            continue

        move_bps = (mid_i - oldest_mid) / oldest_mid * 10000.0
        abs_move = abs(move_bps)

        if abs_move < MOVE_THRESHOLD_BPS:
            continue

        # Spread gate
        if min_spread_pts > 0 and spread_i < min_spread_pts:
            continue

        # phi filter
        phi_i = float(phi[i])
        contrarian_dir = -1 if move_bps > 0 else 1

        if phi_mode == "declining":
            if not phi_decl[i]:
                continue
        elif phi_mode == "sign":
            # phi sign agrees with contrarian direction (momentum already reversed)
            if contrarian_dir == 1 and phi_i <= 0:
                continue
            if contrarian_dir == -1 and phi_i >= 0:
                continue
        elif phi_mode == "threshold":
            if abs(phi_i) > phi_threshold:
                continue

        # Enter
        state = "positioned"
        entry_ts = now_ns
        entry_mid = mid_i
        direction = contrarian_dir
        entry_spread = spread_i
        entry_phi = phi_i
        entry_move_bps = move_bps
        max_adverse = 0.0
        max_favorable = 0.0

    return trades


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
def compute_stats(trades: list[CBSTrade], label: str = "") -> dict:
    n = len(trades)
    if n == 0:
        return {"label": label, "n": 0, "avg_pnl_pts": 0.0, "avg_pnl_bps": 0.0,
                "total_pnl_pts": 0.0, "std_pnl": 0.0, "win_rate": 0.0,
                "stop_rate": 0.0, "t_stat": 0.0, "p_value": 1.0,
                "avg_gross_pts": 0.0, "total_pnl_ntd": 0.0,
                "avg_mae_bps": 0.0, "avg_mfe_bps": 0.0}

    pnls = np.array([t.net_pnl_pts for t in trades])
    gross = np.array([t.gross_pnl_pts for t in trades])
    avg_mid = np.mean([t.entry_mid for t in trades])
    maes = np.array([t.max_adverse_bps for t in trades])
    mfes = np.array([t.max_favorable_bps for t in trades])

    avg_pnl = float(pnls.mean())
    std_pnl = float(pnls.std(ddof=1)) if n > 1 else 0.0
    t_stat = avg_pnl / (std_pnl / math.sqrt(n)) if std_pnl > 0 else 0.0
    p_value = float(2 * scipy_stats.t.sf(abs(t_stat), df=n - 1)) if n > 1 else 1.0
    stops = sum(1 for t in trades if t.exit_reason == "stop_loss")

    return {
        "label": label, "n": n,
        "avg_pnl_pts": avg_pnl,
        "avg_pnl_bps": avg_pnl / avg_mid * 10000.0 if avg_mid > 0 else 0.0,
        "total_pnl_pts": float(pnls.sum()),
        "std_pnl": std_pnl,
        "win_rate": sum(1 for p in pnls if p > 0) / n,
        "stop_rate": stops / n,
        "t_stat": t_stat,
        "p_value": p_value,
        "avg_gross_pts": float(gross.mean()),
        "total_pnl_ntd": float(pnls.sum() * PT_VALUE_NTD),
        "avg_mae_bps": float(maes.mean()),
        "avg_mfe_bps": float(mfes.mean()),
    }


def print_row(s: dict, indent: str = "  ") -> None:
    print(f"{indent}{s['label']:<50s} N={s['n']:>4d}  "
          f"avg={s['avg_pnl_pts']:>+7.2f}pts  "
          f"WR={s['win_rate']:>5.1%}  SR={s['stop_rate']:>5.1%}  "
          f"t={s['t_stat']:>+5.2f}  p={s['p_value']:>.3f}  "
          f"total={s['total_pnl_pts']:>+8.1f}pts")


def run_experiment(day_data, label, **kwargs):
    all_trades = []
    for d, data in sorted(day_data.items()):
        all_trades.extend(run_cbs_backtest(data, d, **kwargs))
    is_t = [t for t in all_trades if t.day in IS_DATES]
    oos_t = [t for t in all_trades if t.day in OOS_DATES]
    return all_trades, compute_stats(is_t, f"{label} IS"), compute_stats(oos_t, f"{label} OOS")


def main():
    print("=" * 110)
    print("CBS ENHANCED BACKTEST v2 — Direction B (Refined)")
    print("=" * 110)

    day_data = load_all_days()
    n_is = sum(1 for d in day_data if d in IS_DATES)
    n_oos = sum(1 for d in day_data if d in OOS_DATES)
    print(f"Loaded {len(day_data)} days (IS: {n_is}, OOS: {n_oos})")
    for d in sorted(day_data.keys()):
        spread = day_data[d]["ask_px"] - day_data[d]["bid_px"]
        print(f"  {d}: {len(day_data[d]):>9,} rows, med_spread={np.median(spread):.0f}")

    # ===================================================================
    # PART 1: Stop-loss sweep (the key finding)
    # ===================================================================
    print("\n" + "=" * 110)
    print("PART 1: STOP-LOSS SWEEP")
    print("Hypothesis: 15 bps stop is cutting mean-reverting trades too early")
    print("=" * 110)

    stop_configs = [
        (True, 0, "NO_STOP"),
        (False, 15, "STOP-15bps (base)"),
        (False, 30, "STOP-30bps"),
        (False, 50, "STOP-50bps"),
        (False, 75, "STOP-75bps"),
        (False, 100, "STOP-100bps"),
        (False, 200, "STOP-200bps"),
    ]

    stop_results = []
    for no_stop, stop_bps, label in stop_configs:
        trades, is_s, oos_s = run_experiment(
            day_data, label,
            stop_bps=stop_bps, no_stop=no_stop,
        )
        stop_results.append((label, is_s, oos_s, trades))
        print_row(is_s)
        print_row(oos_s)

        # Per-day OOS
        oos_t = [t for t in trades if t.day in OOS_DATES]
        days_oos = sorted(set(t.day for t in oos_t))
        for dd in days_oos:
            dt = [t for t in oos_t if t.day == dd]
            pnls = [t.net_pnl_pts for t in dt]
            stops = sum(1 for t in dt if t.exit_reason == "stop_loss")
            print(f"      {dd}: N={len(dt):>3d}  avg={np.mean(pnls):>+7.2f}  "
                  f"total={sum(pnls):>+8.1f}  stops={stops}")
        print()

    # ===================================================================
    # PART 1b: Stopped trade analysis
    # ===================================================================
    print("\n" + "=" * 110)
    print("PART 1b: STOPPED-TRADE ANALYSIS")
    print("What would happen to base stop-loss trades if we held to maturity?")
    print("=" * 110)

    # Run with no stop and base stop, compare MAE/MFE
    _, _, _, no_stop_trades = stop_results[0]  # NO_STOP
    _, _, _, base_stop_trades = stop_results[1]  # STOP-15

    # For stopped trades in base, what's the MAE/MFE?
    stopped = [t for t in base_stop_trades if t.exit_reason == "stop_loss"]
    time_exit = [t for t in base_stop_trades if t.exit_reason == "time_exit"]

    if stopped:
        s_pnls = np.array([t.net_pnl_pts for t in stopped])
        s_maes = np.array([t.max_adverse_bps for t in stopped])
        s_mfes = np.array([t.max_favorable_bps for t in stopped])
        print(f"  Stopped trades (15 bps stop): N={len(stopped)}")
        print(f"    avg PnL at stop: {s_pnls.mean():+.2f} pts")
        print(f"    avg MAE: {s_maes.mean():.1f} bps, avg MFE: {s_mfes.mean():.1f} bps")
        print(f"    MFE > 15 bps in {100*np.mean(s_mfes > 15):.0f}% of stopped trades")
        print(f"    (meaning these trades would have recovered if held)")

    if time_exit:
        t_pnls = np.array([t.net_pnl_pts for t in time_exit])
        t_maes = np.array([t.max_adverse_bps for t in time_exit])
        print(f"\n  Time-exit trades: N={len(time_exit)}")
        print(f"    avg PnL: {t_pnls.mean():+.2f} pts")
        print(f"    avg MAE: {t_maes.mean():.1f} bps")
        print(f"    MAE > 15 bps in {100*np.mean(t_maes > 15):.0f}% (would have been stopped)")

    # Compare no-stop vs base MAE distributions
    ns_all = [t for t in no_stop_trades]
    ns_maes = np.array([t.max_adverse_bps for t in ns_all])
    ns_pnls = np.array([t.net_pnl_pts for t in ns_all])
    ns_mfes = np.array([t.max_favorable_bps for t in ns_all])

    print(f"\n  No-stop trades: N={len(ns_all)}")
    print(f"    avg PnL: {ns_pnls.mean():+.2f} pts")
    print(f"    MAE distribution: p25={np.percentile(ns_maes,25):.1f}, "
          f"p50={np.median(ns_maes):.1f}, p75={np.percentile(ns_maes,75):.1f}, "
          f"p95={np.percentile(ns_maes,95):.1f}")
    print(f"    MFE distribution: p25={np.percentile(ns_mfes,25):.1f}, "
          f"p50={np.median(ns_mfes):.1f}, p75={np.percentile(ns_mfes,75):.1f}")

    # Conditional analysis: trades that drawdown > 15bps but hold
    deep_draw = [t for t in ns_all if t.max_adverse_bps > 15]
    if deep_draw:
        dd_pnls = np.array([t.net_pnl_pts for t in deep_draw])
        print(f"\n  Trades with MAE > 15 bps (would have been stopped): N={len(deep_draw)}")
        print(f"    avg final PnL: {dd_pnls.mean():+.2f} pts")
        print(f"    win rate: {100*(dd_pnls > 0).sum()/len(dd_pnls):.0f}%")
        print(f"    These trades RECOVER {100*(dd_pnls > 0).sum()/len(dd_pnls):.0f}% of the time")

    # ===================================================================
    # PART 2: phi_8min filter (re-calibrated)
    # ===================================================================
    print("\n" + "=" * 110)
    print("PART 2: phi_8min FILTER (with NO stop-loss, since that's better)")
    print("=" * 110)

    # First, diagnostic: phi distribution
    base_no_stop = no_stop_trades
    phis_all = np.array([t.phi_8min_at_entry for t in base_no_stop])
    pnls_all = np.array([t.net_pnl_pts for t in base_no_stop])

    if len(phis_all) > 0:
        print(f"\n  phi_8min at entry: mean={phis_all.mean():+.6f}, "
              f"std={phis_all.std():.6f}, "
              f"p5={np.percentile(phis_all,5):+.6f}, "
              f"p95={np.percentile(phis_all,95):+.6f}")

        # Correlation
        if len(phis_all) > 2:
            corr = np.corrcoef(phis_all, pnls_all)[0, 1]
            print(f"  Correlation(phi_8min, PnL): r={corr:+.4f}")

        # Decile analysis
        print("\n  Decile analysis:")
        pcts = np.percentile(phis_all, np.arange(10, 100, 10))
        bounds = [(-np.inf,)] + [(p,) for p in pcts] + [(np.inf,)]
        for qi in range(10):
            lo = -np.inf if qi == 0 else pcts[qi - 1]
            hi = pcts[qi] if qi < 9 else np.inf
            if qi < 9:
                mask = (phis_all >= lo) & (phis_all < hi)
            else:
                mask = phis_all >= lo
            q_pnls = pnls_all[mask]
            if len(q_pnls) > 0:
                print(f"    D{qi+1:>2d} [{lo:>+10.5f}, {hi:>+10.5f}): "
                      f"N={len(q_pnls):>3d}  avg={q_pnls.mean():>+7.2f}  "
                      f"WR={100*(q_pnls>0).sum()/len(q_pnls):.0f}%")

    # Test phi configs with no stop
    phi_configs = [
        ("declining", 0.0, "phi-declining"),
        ("sign", 0.0, "phi-sign"),
        ("threshold", 0.01, "phi-thresh-0.01"),
        ("threshold", 0.02, "phi-thresh-0.02"),
        ("threshold", 0.05, "phi-thresh-0.05"),
        ("threshold", 0.1, "phi-thresh-0.1"),
    ]

    phi_results = []
    best_phi_oos = -999.0
    best_phi_cfg = None

    for mode, thresh, label in phi_configs:
        trades, is_s, oos_s = run_experiment(
            day_data, label,
            no_stop=True,
            phi_mode=mode, phi_threshold=thresh,
        )
        phi_results.append((label, is_s, oos_s, trades))
        print_row(is_s)
        print_row(oos_s)
        print()

        if oos_s["avg_pnl_pts"] > best_phi_oos and oos_s["n"] >= 5:
            best_phi_oos = oos_s["avg_pnl_pts"]
            best_phi_cfg = (mode, thresh, label)

    print(f"  Best phi (OOS, N>=5): {best_phi_cfg[2] if best_phi_cfg else 'none'} "
          f"({best_phi_oos:+.2f} pts)")

    # ===================================================================
    # PART 3: Spread gate (with no stop)
    # ===================================================================
    print("\n" + "=" * 110)
    print("PART 3: SPREAD GATE (with NO stop-loss)")
    print("=" * 110)

    # Spread distribution at entry
    spreads_at_entry = np.array([t.spread_at_entry for t in base_no_stop])
    print(f"  Spread at entry: mean={spreads_at_entry.mean():.1f}, "
          f"median={np.median(spreads_at_entry):.0f}, "
          f"p25={np.percentile(spreads_at_entry,25):.0f}, "
          f"p75={np.percentile(spreads_at_entry,75):.0f}")

    # Spread bucket analysis
    print("\n  Spread bucket PnL (no stop):")
    for lo, hi, lbl in [(0, 3, "0-2"), (3, 5, "3-4"), (5, 10, "5-9"),
                         (10, 20, "10-19"), (20, 999, "20+")]:
        mask = (spreads_at_entry >= lo) & (spreads_at_entry < hi)
        sp_pnls = pnls_all[mask]
        if len(sp_pnls) > 0:
            print(f"    Spread {lbl:>5s}: N={len(sp_pnls):>3d}  "
                  f"avg={sp_pnls.mean():>+7.2f}  WR={100*(sp_pnls>0).sum()/len(sp_pnls):.0f}%")

    spread_gates = [0, 3, 5, 7, 10]
    spread_results = []
    best_sg_oos = -999.0
    best_sg_cfg = None

    for sg in spread_gates:
        label = f"SG-{sg}"
        trades, is_s, oos_s = run_experiment(
            day_data, label,
            no_stop=True, min_spread_pts=float(sg),
        )
        spread_results.append((label, is_s, oos_s, trades))
        print_row(is_s)
        print_row(oos_s)
        print()

        if oos_s["avg_pnl_pts"] > best_sg_oos and oos_s["n"] >= 3:
            best_sg_oos = oos_s["avg_pnl_pts"]
            best_sg_cfg = (sg, label)

    print(f"  Best spread gate (OOS, N>=3): {best_sg_cfg[1] if best_sg_cfg else 'none'} "
          f"({best_sg_oos:+.2f} pts)")

    # ===================================================================
    # PART 4: Combinations
    # ===================================================================
    print("\n" + "=" * 110)
    print("PART 4: COMBINATIONS (all with no stop)")
    print("=" * 110)

    combos = []

    # No-stop baseline
    combos.append(("NO_STOP baseline", {"no_stop": True}))

    # Best phi
    if best_phi_cfg:
        combos.append((f"NO_STOP + {best_phi_cfg[2]}", {
            "no_stop": True,
            "phi_mode": best_phi_cfg[0],
            "phi_threshold": best_phi_cfg[1],
        }))

    # Best spread gate
    if best_sg_cfg and best_sg_cfg[0] > 0:
        combos.append((f"NO_STOP + {best_sg_cfg[1]}", {
            "no_stop": True,
            "min_spread_pts": float(best_sg_cfg[0]),
        }))

    # phi + spread
    if best_phi_cfg and best_sg_cfg and best_sg_cfg[0] > 0:
        combos.append((f"NO_STOP + {best_phi_cfg[2]} + {best_sg_cfg[1]}", {
            "no_stop": True,
            "phi_mode": best_phi_cfg[0],
            "phi_threshold": best_phi_cfg[1],
            "min_spread_pts": float(best_sg_cfg[0]),
        }))

    # Also test with moderate stop (50 bps)
    combos.append(("STOP-50 baseline", {"stop_bps": 50}))
    if best_phi_cfg:
        combos.append((f"STOP-50 + {best_phi_cfg[2]}", {
            "stop_bps": 50,
            "phi_mode": best_phi_cfg[0],
            "phi_threshold": best_phi_cfg[1],
        }))

    combo_results = []
    for label, kwargs in combos:
        trades, is_s, oos_s = run_experiment(day_data, label, **kwargs)
        combo_results.append((label, is_s, oos_s, trades))
        print_row(is_s)
        print_row(oos_s)

        oos_t = [t for t in trades if t.day in OOS_DATES]
        for dd in sorted(set(t.day for t in oos_t)):
            dt = [t for t in oos_t if t.day == dd]
            pnls = [t.net_pnl_pts for t in dt]
            stops = sum(1 for t in dt if t.exit_reason == "stop_loss")
            print(f"      {dd}: N={len(dt):>3d}  avg={np.mean(pnls):>+7.2f}  "
                  f"total={sum(pnls):>+8.1f}  stops={stops}  WR={sum(1 for p in pnls if p > 0)/len(pnls):.0%}")
        print()

    # ===================================================================
    # SUMMARY
    # ===================================================================
    print("\n" + "=" * 110)
    print("FINAL SUMMARY TABLE")
    print("=" * 110)
    print(f"{'Config':<55s} {'IS N':>5s} {'IS avg':>8s} {'IS t':>6s} "
          f"{'OOS N':>5s} {'OOS avg':>8s} {'OOS t':>6s} {'OOS p':>7s} "
          f"{'SR':>5s} {'WR':>5s} {'delta':>7s}")
    print("-" * 110)

    base_oos_avg = stop_results[1][2]["avg_pnl_pts"]  # STOP-15 baseline

    all_results = []
    all_results.extend(stop_results)
    all_results.extend(phi_results)
    all_results.extend(spread_results)
    all_results.extend(combo_results)

    for label, is_s, oos_s, _ in all_results:
        delta = oos_s["avg_pnl_pts"] - base_oos_avg
        print(f"{label:55s} {is_s['n']:>5d} {is_s['avg_pnl_pts']:>+7.2f} {is_s['t_stat']:>+6.2f} "
              f"{oos_s['n']:>5d} {oos_s['avg_pnl_pts']:>+7.2f} {oos_s['t_stat']:>+6.2f} "
              f"{oos_s['p_value']:>7.3f} {oos_s['stop_rate']:>4.0%} {oos_s['win_rate']:>4.0%} "
              f"{delta:>+7.2f}")

    # ===================================================================
    # KEY INSIGHTS
    # ===================================================================
    print("\n" + "=" * 110)
    print("KEY INSIGHTS")
    print("=" * 110)

    # 1. Stop-loss effect
    no_stop_oos = stop_results[0][2]
    base_oos = stop_results[1][2]
    print(f"\n  1. STOP-LOSS EFFECT:")
    print(f"     Base (15 bps stop): {base_oos['avg_pnl_pts']:+.2f} pts, SR={base_oos['stop_rate']:.0%}")
    print(f"     No stop:            {no_stop_oos['avg_pnl_pts']:+.2f} pts")
    print(f"     Delta:              {no_stop_oos['avg_pnl_pts'] - base_oos['avg_pnl_pts']:+.2f} pts")
    print(f"     The 15 bps stop-loss DESTROYS {base_oos['avg_pnl_pts'] - no_stop_oos['avg_pnl_pts']:.2f} pts/trade")

    print(f"\n  2. STATISTICAL SIGNIFICANCE:")
    print(f"     Best OOS t-stat: {max((r[2]['t_stat'] for _, _, _, r_unused in [] or [(None, None, s, None) for _, _, s, _ in all_results]), default=0)}")
    # Find best config
    best_cfg = max(all_results, key=lambda x: x[2]["avg_pnl_pts"])
    print(f"     Best config: {best_cfg[0]}")
    print(f"     OOS: avg={best_cfg[2]['avg_pnl_pts']:+.2f}, t={best_cfg[2]['t_stat']:+.2f}, p={best_cfg[2]['p_value']:.3f}")

    print(f"\n  3. CONCLUSION:")
    if best_cfg[2]["p_value"] < 0.05:
        print(f"     PASS — statistically significant at 5% level")
    elif best_cfg[2]["avg_pnl_pts"] > 0:
        print(f"     CONDITIONAL — positive OOS but not statistically significant (p={best_cfg[2]['p_value']:.3f})")
        print(f"     Need more data or higher N to reach significance")
    else:
        print(f"     FAIL — no config produces reliably positive OOS returns")

    print("\n" + "=" * 110)
    print("DONE")
    print("=" * 110)


if __name__ == "__main__":
    main()
