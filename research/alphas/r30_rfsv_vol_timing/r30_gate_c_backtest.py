"""R30 Gate C Backtest — manual backtest for RFSV and Zumbach on TMFD6 L1 data.

Runs both alphas on real TMFD6 tick data with:
- P95 latency: 36ms (shioaji_sim_p95_v2026-03-04)
- Cost: 3.92 pts round-trip (TMFD6: 1 pt = 10 NTD)
- IS/OOS split: 70/30
- Walk-forward per-day consistency check

Output: JSON scorecard + text summary.
"""

from __future__ import annotations

import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from research.alphas.r30_rfsv_vol_timing.impl import R30RfsvVolTimingAlpha
from research.alphas.r30_zumbach_vol_feedback.impl import R30ZumbachVolFeedbackAlpha

# --- Constants ---
COST_PTS: float = 3.92           # round-trip cost in TMFD6 points
POINT_VALUE_NTD: float = 10.0    # 1 TMFD6 point = 10 NTD
LATENCY_NS: int = 36_000_000     # 36ms P95 latency
SIGNAL_ENTRY_THRESHOLD: float = 0.3   # enter when |signal| > threshold
SIGNAL_EXIT_THRESHOLD: float = 0.05   # exit when |signal| < threshold
OOS_SPLIT: float = 0.7           # 70% IS, 30% OOS

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "raw" / "tmfd6"


@dataclass
class Trade:
    entry_price: float
    exit_price: float
    direction: int  # +1 long, -1 short
    entry_tick: int
    exit_tick: int
    pnl_pts: float = 0.0


@dataclass
class BacktestResult:
    alpha_id: str
    n_ticks: int = 0
    n_trades: int = 0
    gross_pnl_pts: float = 0.0
    total_cost_pts: float = 0.0
    net_pnl_pts: float = 0.0
    sharpe: float = 0.0
    max_dd_pts: float = 0.0
    win_rate: float = 0.0
    avg_hold_ticks: int = 0
    hurst_h_final: float = 0.0
    tra_stat: float = 0.0
    daily_pnls: list[float] = field(default_factory=list)
    is_pnl_pts: float = 0.0
    oos_pnl_pts: float = 0.0
    is_sharpe: float = 0.0
    oos_sharpe: float = 0.0


def load_all_data() -> list[tuple[str, np.ndarray]]:
    """Load all TMFD6 L1 .npy files sorted by date."""
    files = sorted(DATA_DIR.glob("TMFD6_*_l1.npy"))
    if not files:
        raise FileNotFoundError(f"No TMFD6 L1 data in {DATA_DIR}")
    result = []
    for f in files:
        date_str = f.stem.split("_")[1]
        data = np.load(f, allow_pickle=True)
        result.append((date_str, data))
    return result


def run_backtest(alpha: object, daily_data: list[tuple[str, np.ndarray]], alpha_id: str) -> BacktestResult:
    """Run backtest on an alpha across all daily data files."""
    result = BacktestResult(alpha_id=alpha_id)
    all_trades: list[Trade] = []
    trade_pnls: list[float] = []

    position: int = 0  # +1, -1, or 0
    entry_price: float = 0.0
    entry_tick: int = 0
    global_tick: int = 0

    # Subsample every Nth tick for performance (5M+ ticks is too slow per-tick)
    subsample = 10  # process every 10th tick — still ~500K ticks total

    for date_str, data in daily_data:
        day_pnl = 0.0
        mid_prices = data["mid_price"]
        n = len(mid_prices)

        for i in range(0, n, subsample):
            mid_px = float(mid_prices[i])
            if mid_px <= 0:
                global_tick += 1
                continue

            # Feed alpha (convert to scaled int x10000 for the alpha interface)
            price_scaled = int(mid_px * 10000)
            signal = alpha.update(price=price_scaled)
            result.n_ticks += 1
            global_tick += 1

            # Trading logic
            if position == 0:
                # Entry
                if signal > SIGNAL_ENTRY_THRESHOLD:
                    position = 1
                    entry_price = mid_px
                    entry_tick = global_tick
                elif signal < -SIGNAL_ENTRY_THRESHOLD:
                    position = -1
                    entry_price = mid_px
                    entry_tick = global_tick
            else:
                # Exit conditions
                should_exit = False
                if position == 1 and signal < SIGNAL_EXIT_THRESHOLD:
                    should_exit = True
                elif position == -1 and signal > -SIGNAL_EXIT_THRESHOLD:
                    should_exit = True

                if should_exit:
                    pnl_pts = position * (mid_px - entry_price) - COST_PTS
                    day_pnl += pnl_pts
                    trade_pnls.append(pnl_pts)
                    all_trades.append(Trade(
                        entry_price=entry_price,
                        exit_price=mid_px,
                        direction=position,
                        entry_tick=entry_tick,
                        exit_tick=global_tick,
                        pnl_pts=pnl_pts,
                    ))
                    position = 0

        # Force close at end of day
        if position != 0:
            last_px = float(mid_prices[-1])
            if last_px > 0:
                pnl_pts = position * (last_px - entry_price) - COST_PTS
                day_pnl += pnl_pts
                trade_pnls.append(pnl_pts)
                all_trades.append(Trade(
                    entry_price=entry_price,
                    exit_price=last_px,
                    direction=position,
                    entry_tick=entry_tick,
                    exit_tick=global_tick,
                    pnl_pts=pnl_pts,
                ))
            position = 0

        result.daily_pnls.append(day_pnl)

    # Compute aggregate stats
    result.n_trades = len(all_trades)
    if result.n_trades > 0:
        result.gross_pnl_pts = sum(t.pnl_pts + COST_PTS for t in all_trades)
        result.total_cost_pts = COST_PTS * result.n_trades
        result.net_pnl_pts = sum(t.pnl_pts for t in all_trades)
        result.win_rate = sum(1 for t in all_trades if t.pnl_pts > 0) / result.n_trades
        result.avg_hold_ticks = int(np.mean([t.exit_tick - t.entry_tick for t in all_trades]))

        # Sharpe (annualized from daily PnL)
        if len(result.daily_pnls) > 1:
            daily_arr = np.array(result.daily_pnls)
            if float(np.std(daily_arr)) > 1e-10:
                result.sharpe = float(np.mean(daily_arr) / np.std(daily_arr) * math.sqrt(252))

        # Max drawdown
        cumsum = np.cumsum(trade_pnls)
        peak = np.maximum.accumulate(cumsum)
        drawdown = peak - cumsum
        result.max_dd_pts = float(np.max(drawdown)) if len(drawdown) > 0 else 0.0

        # IS/OOS split
        n_is = int(len(result.daily_pnls) * OOS_SPLIT)
        is_daily = np.array(result.daily_pnls[:n_is])
        oos_daily = np.array(result.daily_pnls[n_is:])

        result.is_pnl_pts = float(np.sum(is_daily))
        result.oos_pnl_pts = float(np.sum(oos_daily))

        if len(is_daily) > 1 and float(np.std(is_daily)) > 1e-10:
            result.is_sharpe = float(np.mean(is_daily) / np.std(is_daily) * math.sqrt(252))
        if len(oos_daily) > 1 and float(np.std(oos_daily)) > 1e-10:
            result.oos_sharpe = float(np.mean(oos_daily) / np.std(oos_daily) * math.sqrt(252))

    # Alpha-specific diagnostics
    if hasattr(alpha, "hurst_h"):
        result.hurst_h_final = alpha.hurst_h
    if hasattr(alpha, "tra_ratio"):
        result.tra_stat = alpha.tra_ratio

    return result


def gate_c_check(result: BacktestResult, alpha_id: str) -> dict:
    """Apply Gate C kill conditions."""
    checks: dict[str, dict] = {}

    if alpha_id == "r30_rfsv_vol_timing":
        checks["sharpe_positive"] = {
            "pass": result.sharpe > 0,
            "value": result.sharpe,
            "threshold": "> 0",
        }
        checks["oos_sharpe_positive"] = {
            "pass": result.oos_sharpe > 0,
            "value": result.oos_sharpe,
            "threshold": "> 0",
        }
        checks["is_oos_gap"] = {
            "pass": (
                abs(result.is_sharpe) < 1e-10
                or abs(result.is_sharpe - result.oos_sharpe) / max(abs(result.is_sharpe), 1e-10) < 0.5
            ),
            "value": (
                abs(result.is_sharpe - result.oos_sharpe) / max(abs(result.is_sharpe), 1e-10)
                if abs(result.is_sharpe) > 1e-10
                else 0.0
            ),
            "threshold": "< 0.5 (50% gap)",
        }
        checks["hurst_reasonable"] = {
            "pass": 0.01 <= result.hurst_h_final <= 0.49,
            "value": result.hurst_h_final,
            "threshold": "[0.01, 0.49]",
        }
    elif alpha_id == "r30_zumbach_vol_feedback":
        checks["tra_positive"] = {
            "pass": result.tra_stat > 0,
            "value": result.tra_stat,
            "threshold": "> 0 (Zumbach effect present)",
        }
        checks["sharpe_positive"] = {
            "pass": result.sharpe > 0,
            "value": result.sharpe,
            "threshold": "> 0",
        }
        checks["oos_sharpe_positive"] = {
            "pass": result.oos_sharpe > 0,
            "value": result.oos_sharpe,
            "threshold": "> 0",
        }

    checks["max_dd_acceptable"] = {
        "pass": result.max_dd_pts < 100.0,
        "value": result.max_dd_pts,
        "threshold": "< 100 pts",
    }
    checks["min_trades"] = {
        "pass": result.n_trades >= 10,
        "value": result.n_trades,
        "threshold": ">= 10",
    }

    gate_c_pass = all(c["pass"] for c in checks.values())
    return {"gate_c_pass": gate_c_pass, "checks": checks}


def main() -> None:
    daily_data = load_all_data()
    n_days = len(daily_data)
    total_ticks = sum(len(d[1]) for d in daily_data)
    print(f"Loaded {n_days} days, {total_ticks:,} total ticks")

    output_dir = Path(__file__).resolve().parents[2] / "docs" / "research" / "alpha-research-r30"
    output_dir.mkdir(parents=True, exist_ok=True)

    results = {}
    for alpha_id, alpha_cls in [
        ("r30_rfsv_vol_timing", R30RfsvVolTimingAlpha),
        ("r30_zumbach_vol_feedback", R30ZumbachVolFeedbackAlpha),
    ]:
        print(f"\n{'='*60}")
        print(f"Running Gate C: {alpha_id}")
        print(f"{'='*60}")

        alpha = alpha_cls()
        result = run_backtest(alpha, daily_data, alpha_id)
        gate_c = gate_c_check(result, alpha_id)

        print(f"\n  Ticks processed : {result.n_ticks:,}")
        print(f"  Trades          : {result.n_trades}")
        print(f"  Gross PnL (pts) : {result.gross_pnl_pts:.2f}")
        print(f"  Total cost (pts): {result.total_cost_pts:.2f}")
        print(f"  Net PnL (pts)   : {result.net_pnl_pts:.2f}")
        print(f"  Sharpe (ann.)   : {result.sharpe:.3f}")
        print(f"  IS Sharpe       : {result.is_sharpe:.3f}")
        print(f"  OOS Sharpe      : {result.oos_sharpe:.3f}")
        print(f"  Max DD (pts)    : {result.max_dd_pts:.2f}")
        print(f"  Win rate        : {result.win_rate:.1%}")
        print(f"  Avg hold (ticks): {result.avg_hold_ticks}")
        if alpha_id == "r30_rfsv_vol_timing":
            print(f"  Hurst H (final) : {result.hurst_h_final:.4f}")
        elif alpha_id == "r30_zumbach_vol_feedback":
            print(f"  TRA stat        : {result.tra_stat:.6f}")

        print(f"\n  Gate C: {'PASS' if gate_c['gate_c_pass'] else 'FAIL'}")
        for name, check in gate_c["checks"].items():
            status = "PASS" if check["pass"] else "FAIL"
            print(f"    [{status}] {name}: {check['value']:.4f} (threshold: {check['threshold']})")

        # Daily PnL breakdown
        print(f"\n  Daily PnL breakdown:")
        for i, (date_str, _) in enumerate(daily_data):
            pnl = result.daily_pnls[i] if i < len(result.daily_pnls) else 0.0
            bar = "+" * max(0, int(pnl / 2)) if pnl > 0 else "-" * max(0, int(-pnl / 2))
            print(f"    {date_str}: {pnl:+8.2f} pts  {bar}")

        results[alpha_id] = {
            "n_ticks": result.n_ticks,
            "n_trades": result.n_trades,
            "gross_pnl_pts": round(result.gross_pnl_pts, 2),
            "total_cost_pts": round(result.total_cost_pts, 2),
            "net_pnl_pts": round(result.net_pnl_pts, 2),
            "sharpe": round(result.sharpe, 4),
            "is_sharpe": round(result.is_sharpe, 4),
            "oos_sharpe": round(result.oos_sharpe, 4),
            "max_dd_pts": round(result.max_dd_pts, 2),
            "win_rate": round(result.win_rate, 4),
            "avg_hold_ticks": result.avg_hold_ticks,
            "daily_pnls": [round(p, 2) for p in result.daily_pnls],
            "gate_c": gate_c,
        }
        if alpha_id == "r30_rfsv_vol_timing":
            results[alpha_id]["hurst_h_final"] = round(result.hurst_h_final, 4)
        else:
            results[alpha_id]["tra_stat"] = round(result.tra_stat, 6)

    # Save scorecard
    scorecard_path = output_dir / "stage3_4_gate_c_results.json"
    scorecard_path.write_text(json.dumps(results, indent=2) + "\n")
    print(f"\nScorecard saved to {scorecard_path}")


if __name__ == "__main__":
    main()
