"""Driver: faithful backtest of the Zeiierman 'AI Source Switching Moving
Average' strategy on TXF bars. The directional signal is the AI Supertrend flip.

No parameter tuning. Reports gross and net at multiple cost levels and both
long+short and long-only, with an in-sample / out-of-sample split. Execution is
no-lookahead: a flip at a confirmed bar close fills at the NEXT bar's open;
open positions force-flat at each session close (intraday discipline). The
beta-neutral permutation test isolates timing alpha from market drift.

Run:
  uv run python -m research.experiments.validations.ai_ssma_zeiierman_v0.ai_ssma_backtest \
      --bar-min 5 --contracts txfd6,txfe6,txff6,txfg6 --session day
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

# reuse the vetted bar reconstruction + position/metric/beta-neutral apparatus
from research.experiments.validations.ml_rsi_zeiierman_v0.bars import build_bars
from research.experiments.validations.ml_rsi_zeiierman_v0.ml_rsi_backtest import (
    COST_LEVELS,
    OOS_START,
    _beta_neutral,
    _report_block,
    _run_positions,
)

from .indicator import compute_indicator

RAW_DIR = Path("research/data/raw")
OUT_DIR = Path("research/experiments/validations/ai_ssma_zeiierman_v0")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar-min", type=int, default=5)
    ap.add_argument("--contracts", type=str, default="txfd6,txfe6,txff6,txfg6")
    ap.add_argument("--session", type=str, default="day", choices=["day", "night"])
    ap.add_argument("--st-exit", action="store_true", help="(redundant) exit on ST flip")
    args = ap.parse_args()

    contracts = [c.strip() for c in args.contracts.split(",") if c.strip()]
    result = {
        "schema": "research.ai_ssma_zeiierman_backtest.v1",
        "strategy": "Zeiierman AI Source Switching MA -- AI Supertrend flips, published defaults, no tuning",
        "bar_min": args.bar_min,
        "contracts": contracts,
        "session": f"TAIFEX {args.session} session, intraday force-flat",
        "execution": "Supertrend flip at confirmed bar close -> fill next bar open; force-flat at session close",
        "exit_policy": "signal_flip",
        "cost_levels_pts": list(COST_LEVELS),
        "oos_start": OOS_START,
        "instrument_note": "signals & fills on TXF itself; 8pt column is the platform TXF→TMF envelope (frozen)",
        "per_contract": {},
        "pooled": {},
    }

    pooled_both, pooled_long = [], []
    bn_long_excess = bn_both_excess = 0.0
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
        result["per_contract"][c] = {
            "n_bars": len(bars.close),
            "n_days": int(np.unique(bars.date).size),
            "signals": ind.diag,
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

    suffix = f"{args.bar_min}m_{args.session}_flip"
    out_path = OUT_DIR / f"ai_ssma_backtest_{suffix}.json"
    out_path.write_text(json.dumps(result, indent=2))

    print(f"=== Zeiierman AI-SSMA | {args.bar_min}m {args.session} bars | exit=flip ===")
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
    lo_e, lo_t = bn["long_only_excess_per_trade_pts"], bn["long_only_excess_total_pts"]
    ls_e, ls_t = bn["long_short_excess_per_trade_pts"], bn["long_short_excess_total_pts"]
    print("\n[beta-neutral: timing alpha after removing market drift]")
    print(f"  long_only  excess/trade={lo_e} pts  total={lo_t}")
    print(f"  long_short excess/trade={ls_e} pts  total={ls_t}")
    for c, cell in result["per_contract"].items():
        if "beta_neutral_long_only" in cell:
            b = cell["beta_neutral_long_only"]
            sc = cell["signals"].get("best_src_counts", {})
            print(
                f"  {c}: exposure={b['exposure_frac']} E[r|long]={b['E_ret_in_long']} "
                f"E[r|out]={b['E_ret_out']} excess/trade={b['excess_per_trade_pts']} "
                f"perm_p={b['perm_p_value_one_sided']} sig={b['timing_alpha_significant_at_0.05']} src={sc}"
            )
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
