"""Driver: faithful backtest of the Zeiierman ML-RSI strategy on TXF day-session bars.

No parameter tuning. Reports gross and net at multiple cost levels and both
long+short and long-only, with an in-sample / out-of-sample split, so the
result is honest at every cost assumption. Execution is no-lookahead: signals
fire at a confirmed bar close and fill at the NEXT bar's open; open positions
force-flat at each day-session close (intraday discipline).

Run:
  uv run python -m research.experiments.validations.ml_rsi_zeiierman_v0.ml_rsi_backtest \
      --bar-min 5 --contracts d6,e6,f6,g6
"""

from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .bars import build_bars
from .indicator import compute_indicator

RAW_DIR = Path("research/data/raw")
OUT_DIR = Path("research/experiments/validations/ml_rsi_zeiierman_v0")
OOS_START = "2026-04-01"
# Cost levels in TXF index points, round-trip:
#   0  = gross (no friction)
#   2  = realistic TXF-direct (≈1pt spread each leg + fees) -- the strategy trades TXF
#   8  = platform TXF→TMF envelope all-in (FROZEN discipline; kept for comparability)
COST_LEVELS = (0.0, 2.0, 8.0)


@dataclass
class Trade:
    dir: int
    gross: float
    date: str
    oos: bool


def _run_positions(bars, ind, *, side: str, st_exit: bool):
    """Signal-flip position model with intraday force-flat. side in {both,long}.

    Returns (trades, pos_active) where pos_active[i] in {-1,0,1} is the position
    HELD DURING bar i (established by the prior bar's decision, filled at open[i]),
    used for the beta-neutral forward-return analysis.
    """
    n = len(bars.close)
    trades: list[Trade] = []
    pos_active = np.zeros(n, dtype=int)
    pos = 0
    entry_px = 0.0
    entry_dir = 0

    def close_trade(exit_px: float, date: str) -> None:
        nonlocal pos, entry_px, entry_dir
        gross = (exit_px - entry_px) if entry_dir == 1 else (entry_px - exit_px)
        trades.append(Trade(dir=entry_dir, gross=float(gross), date=date, oos=date >= OOS_START))
        pos = 0
        entry_dir = 0

    for i in range(n):
        pos_active[i] = pos  # held coming into bar i (decision filled at open[i])
        last_in_session = bars.is_session_close[i] or (i == n - 1)

        # ST trailing-stop exit (optional variant), evaluated at this bar's close
        if st_exit and pos != 0 and not last_in_session:
            if (pos == 1 and ind.st_dir[i] == -1) or (pos == -1 and ind.st_dir[i] == 1):
                close_trade(bars.close[i], bars.date[i])

        if not last_in_session:
            nxt = i + 1
            fill = bars.open[nxt]
            tl, ts = bool(ind.trigger_long[i]), bool(ind.trigger_short[i])
            if pos == 0:
                if tl:
                    pos, entry_px, entry_dir = 1, fill, 1
                elif ts and side == "both":
                    pos, entry_px, entry_dir = -1, fill, -1
            elif pos == 1:
                if ts:
                    close_trade(fill, bars.date[i])
                    if side == "both":
                        pos, entry_px, entry_dir = -1, fill, -1
            elif pos == -1:
                if tl:
                    close_trade(fill, bars.date[i])
                    pos, entry_px, entry_dir = 1, fill, 1

        if last_in_session and pos != 0:
            close_trade(bars.close[i], bars.date[i])

    return trades, pos_active


def _beta_neutral(bars, pos_active: np.ndarray, *, side: str, n_trades: int, n_perm: int = 3000) -> dict:
    """Isolate timing alpha from market drift.

    Uses same-session close-to-close bar returns r[i] and the position held during
    bar i. The bull-market beta lives in E[r] (unconditional, > 0 here). The
    question is whether the strategy concentrates exposure in higher-return bars:
      timing_edge = E[r | in-position, signed] - exposure_frac * E[r]
    A permutation test shuffles the position labels against the returns (fixed RNG
    seed for determinism) to get a one-sided p-value: would a random selection of
    the same number/sign of bars do as well by chance?
    """
    n = len(bars.close)
    r_list, a_list = [], []
    for i in range(1, n):
        if bars.date[i] != bars.date[i - 1]:
            continue  # skip cross-session gaps (no overnight hold)
        r_list.append(bars.close[i] - bars.close[i - 1])
        a_list.append(int(pos_active[i]))
    if not r_list:
        return {"n_bars": 0}
    r = np.array(r_list, dtype=float)
    a = np.array(a_list, dtype=int)
    if side == "long":
        a = np.where(a > 0, 1, 0)  # long-only exposure
    exposure = float(np.mean(a != 0))
    mkt_mean = float(r.mean())
    strat = a * r  # signed per-bar strategy return
    strat_mean = float(strat.mean())
    # excess over a same-exposure beta portfolio
    excess_per_bar = strat_mean - exposure * mkt_mean
    in_mask = a != 0
    e_in = float(r[a == 1].mean()) if np.any(a == 1) else float("nan")
    e_out = float(r[~in_mask].mean()) if np.any(~in_mask) else float("nan")
    n_in = int(in_mask.sum())

    # deterministic permutation test on the signed strategy mean
    rng = np.random.default_rng(20260609)
    obs = strat_mean
    ge = 0
    for _ in range(n_perm):
        perm = rng.permutation(a)
        if float((perm * r).mean()) >= obs:
            ge += 1
    p_val = (ge + 1) / (n_perm + 1)

    excess_total = excess_per_bar * len(r)
    return {
        "n_bars": len(r),
        "bars_in_position": n_in,
        "exposure_frac": round(exposure, 3),
        "market_mean_bar_ret": round(mkt_mean, 3),
        "strat_mean_bar_ret": round(strat_mean, 3),
        "E_ret_in_long": round(e_in, 3),
        "E_ret_out": round(e_out, 3),
        "excess_per_bar_vs_beta": round(excess_per_bar, 4),
        "excess_total_pts": round(excess_total, 1),
        "excess_per_trade_pts": round(excess_total / n_trades, 2) if n_trades else None,
        "perm_p_value_one_sided": round(p_val, 4),
        "timing_alpha_significant_at_0.05": p_val < 0.05,
    }


def _metrics(trades: list[Trade], cost: float) -> dict:
    if not trades:
        return {"n": 0}
    nets = [t.gross - cost for t in trades]
    gross = [t.gross for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    cum = np.cumsum(nets)
    peak = np.maximum.accumulate(cum)
    max_dd = float((cum - peak).min()) if len(cum) else 0.0
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
    return {
        "n": len(trades),
        "gross_mean": round(statistics.mean(gross), 3),
        "net_mean": round(statistics.mean(nets), 3),
        "net_median": round(statistics.median(nets), 3),
        "net_total": round(float(sum(nets)), 1),
        "win_rate": round(len(wins) / len(trades), 3),
        "profit_factor": round(pf, 3) if pf != float("inf") else None,
        "max_drawdown_pts": round(max_dd, 1),
    }


def _report_block(trades: list[Trade]) -> dict:
    is_trades = [t for t in trades if not t.oos]
    oos_trades = [t for t in trades if t.oos]
    block = {"by_cost": {}, "n_total": len(trades), "n_is": len(is_trades), "n_oos": len(oos_trades)}
    for c in COST_LEVELS:
        block["by_cost"][f"{c:g}pt"] = {
            "all": _metrics(trades, c),
            "is": _metrics(is_trades, c),
            "oos": _metrics(oos_trades, c),
        }
    return block


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar-min", type=int, default=5)
    ap.add_argument("--contracts", type=str, default="txfd6,txfe6,txff6,txfg6")
    ap.add_argument("--session", type=str, default="day", choices=["day", "night"])
    ap.add_argument("--st-exit", action="store_true", help="use ML-Supertrend trailing-stop exit")
    args = ap.parse_args()

    contracts = [c.strip() for c in args.contracts.split(",") if c.strip()]
    result = {
        "schema": "research.ml_rsi_zeiierman_backtest.v1",
        "strategy": "Zeiierman Machine Learning RSI (KNN analog) -- published defaults, no tuning",
        "bar_min": args.bar_min,
        "contracts": contracts,
        "session": f"TAIFEX {args.session} session, intraday force-flat",
        "execution": "signal at confirmed bar close -> fill next bar open; force-flat at session close",
        "exit_policy": "st_trailing_stop" if args.st_exit else "signal_flip",
        "cost_levels_pts": list(COST_LEVELS),
        "oos_start": OOS_START,
        "instrument_note": "signals & fills on TXF itself; 8pt column is the platform TXF→TMF envelope (frozen)",
        "per_contract": {},
        "pooled": {},
    }

    pooled_both: list[Trade] = []
    pooled_long: list[Trade] = []
    bn_both_excess = 0.0
    bn_long_excess = 0.0
    bn_long_bars = 0
    for c in contracts:
        bars = build_bars(RAW_DIR, c, args.bar_min, session=args.session)
        if len(bars.close) == 0:
            result["per_contract"][c] = {"error": "no bars"}
            continue
        ind = compute_indicator(bars.open, bars.high, bars.low, bars.close)
        both, pa_both = _run_positions(bars, ind, side="both", st_exit=args.st_exit)
        longs, pa_long = _run_positions(bars, ind, side="long", st_exit=args.st_exit)
        pooled_both.extend(both)
        pooled_long.extend(longs)
        bn_long = _beta_neutral(bars, pa_long, side="long", n_trades=len(longs))
        bn_both = _beta_neutral(bars, pa_both, side="both", n_trades=len(both))
        bn_long_excess += bn_long.get("excess_total_pts", 0.0) or 0.0
        bn_both_excess += bn_both.get("excess_total_pts", 0.0) or 0.0
        bn_long_bars += bn_long.get("n_bars", 0)
        result["per_contract"][c] = {
            "n_bars": len(bars.close),
            "n_days": int(np.unique(bars.date).size),
            "triggers": ind.diag,
            "long_short": _report_block(both),
            "long_only": _report_block(longs),
            "beta_neutral_long_only": bn_long,
            "beta_neutral_long_short": bn_both,
        }

    result["pooled"]["long_short"] = _report_block(pooled_both)
    result["pooled"]["long_only"] = _report_block(pooled_long)
    result["pooled"]["beta_neutral_summary"] = {
        "long_only_excess_total_pts": round(bn_long_excess, 1),
        "long_only_excess_per_trade_pts": round(bn_long_excess / max(1, len(pooled_long)), 2),
        "long_short_excess_total_pts": round(bn_both_excess, 1),
        "long_short_excess_per_trade_pts": round(bn_both_excess / max(1, len(pooled_both)), 2),
        "note": "excess = strat bar-return minus same-exposure beta portfolio; isolates timing alpha from drift",
    }

    suffix = f"{args.bar_min}m_{args.session}_{'st' if args.st_exit else 'flip'}"
    out_path = OUT_DIR / f"ml_rsi_backtest_{suffix}.json"
    out_path.write_text(json.dumps(result, indent=2))

    # compact console summary
    print(f"=== Zeiierman ML-RSI | {args.bar_min}m {args.session} bars | exit={result['exit_policy']} ===")
    for variant in ("long_short", "long_only"):
        blk = result["pooled"][variant]
        print(f"\n[{variant}] pooled trades={blk['n_total']} (IS {blk['n_is']} / OOS {blk['n_oos']})")
        for cost_key, cell in blk["by_cost"].items():
            a, o = cell["all"], cell["oos"]
            print(
                f"  cost {cost_key:>4}: ALL net_mean={a.get('net_mean')} med={a.get('net_median')} "
                f"wr={a.get('win_rate')} PF={a.get('profit_factor')} total={a.get('net_total')} "
                f"maxDD={a.get('max_drawdown_pts')} | OOS net_mean={o.get('net_mean')} med={o.get('net_median')}"
            )
    bn = result["pooled"]["beta_neutral_summary"]
    print("\n[beta-neutral: timing alpha after removing market drift]")
    lo_e, lo_t = bn["long_only_excess_per_trade_pts"], bn["long_only_excess_total_pts"]
    ls_e, ls_t = bn["long_short_excess_per_trade_pts"], bn["long_short_excess_total_pts"]
    print(f"  long_only  excess/trade={lo_e} pts  total={lo_t}")
    print(f"  long_short excess/trade={ls_e} pts  total={ls_t}")
    for c, cell in result["per_contract"].items():
        if "beta_neutral_long_only" in cell:
            b = cell["beta_neutral_long_only"]
            print(
                f"  {c}: exposure={b['exposure_frac']} E[r|long]={b['E_ret_in_long']} "
                f"E[r|out]={b['E_ret_out']} excess/trade={b['excess_per_trade_pts']} "
                f"perm_p={b['perm_p_value_one_sided']} sig={b['timing_alpha_significant_at_0.05']}"
            )
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
