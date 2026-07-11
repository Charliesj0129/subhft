"""Driver: faithful backtest of LuxAlgo 'Order Flow VWAP Deviation' on TXF bars.

The indicator has no built-in entries, so four mechanical rules are tested on
its ported order-flow signals, each with a no-lookahead position engine (signal
at confirmed bar close -> fill next bar open; intraday force-flat at session
close). No parameter tuning. Honest scoring at multiple cost levels, IS/OOS
split, long-short and long-only, plus the beta-neutral permutation test that
isolates timing alpha from the bull-tape market drift.

Rules:
  vwap_cross : long on VWAP reclaim, short on VWAP loss        (exit: flip)
  vwap_fade  : long below lower band / short above upper band  (exit: revert to VWAP)
  stop_sweep : long on swept low / short on swept high         (exit: flip)
  ifvg       : long/short on re-entry into an active IFVG zone (exit: flip)

Run:
  uv run python -m research.experiments.validations.of_vwap_dev_luxalgo_v0.of_vwap_backtest \
      --bar-min 5 --contracts txfd6,txfe6,txff6,txfg6 --session day
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from research.experiments.validations.ml_rsi_zeiierman_v0.bars import build_bars
from research.experiments.validations.ml_rsi_zeiierman_v0.ml_rsi_backtest import (
    COST_LEVELS,
    OOS_START,
    Trade,
    _beta_neutral,
    _report_block,
)

from .indicator import compute_signals

RAW_DIR = Path("research/data/raw")
OUT_DIR = Path("research/experiments/validations/of_vwap_dev_luxalgo_v0")

RULES = ("vwap_cross", "vwap_fade", "stop_sweep", "ifvg")


def _entries(sig, rule: str):
    return {
        "vwap_cross": (sig.cross_long, sig.cross_short),
        "vwap_fade": (sig.fade_long, sig.fade_short),
        "stop_sweep": (sig.sweep_long, sig.sweep_short),
        "ifvg": (sig.ifvg_long, sig.ifvg_short),
    }[rule]


def _run_rule(bars, sig, *, rule: str, side: str):
    """No-lookahead position engine. Returns (trades, pos_active).

    Exit policy: 'vwap_fade' is mean-reversion -> exit when close reverts to
    VWAP (no reversal). All others flip on the opposite entry signal. Every rule
    force-flats at the session close.
    """
    long_e, short_e = _entries(sig, rule)
    is_fade = rule == "vwap_fade"
    n = len(bars.close)
    trades: list[Trade] = []
    pos_active = np.zeros(n, dtype=int)
    pos = 0
    entry_px = 0.0
    entry_dir = 0

    def close_trade(exit_px: float, date: str) -> None:
        nonlocal pos, entry_dir
        gross = (exit_px - entry_px) if entry_dir == 1 else (entry_px - exit_px)
        trades.append(Trade(dir=entry_dir, gross=float(gross), date=date, oos=date >= OOS_START))
        pos = 0
        entry_dir = 0

    for i in range(n):
        pos_active[i] = pos
        last = bool(bars.is_session_close[i]) or (i == n - 1)

        if not last:
            nxt = i + 1
            fill = bars.open[nxt]
            tl, ts = bool(long_e[i]), bool(short_e[i])

            # fade target exit: revert to VWAP
            if is_fade and pos != 0 and not np.isnan(sig.vwap[i]):
                if (pos == 1 and bars.close[i] >= sig.vwap[i]) or (pos == -1 and bars.close[i] <= sig.vwap[i]):
                    close_trade(fill, bars.date[i])

            if pos == 0:
                if tl:
                    pos, entry_px, entry_dir = 1, fill, 1
                elif ts and side == "both":
                    pos, entry_px, entry_dir = -1, fill, -1
            elif not is_fade and pos == 1 and ts:
                close_trade(fill, bars.date[i])
                if side == "both":
                    pos, entry_px, entry_dir = -1, fill, -1
            elif not is_fade and pos == -1 and tl:
                close_trade(fill, bars.date[i])
                pos, entry_px, entry_dir = 1, fill, 1

        if last and pos != 0:
            close_trade(bars.close[i], bars.date[i])

    return trades, pos_active


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar-min", type=int, default=5)
    ap.add_argument("--contracts", type=str, default="txfd6,txfe6,txff6,txfg6")
    ap.add_argument("--session", type=str, default="day", choices=["day", "night"])
    args = ap.parse_args()

    contracts = [c.strip() for c in args.contracts.split(",") if c.strip()]
    result = {
        "schema": "research.of_vwap_dev_luxalgo_backtest.v1",
        "strategy": "LuxAlgo Order Flow VWAP Deviation -- 4 mechanical rules, published defaults, no tuning",
        "bar_min": args.bar_min,
        "contracts": contracts,
        "session": f"TAIFEX {args.session} session, intraday force-flat",
        "execution": "signal at confirmed bar close -> fill next bar open; force-flat at session close",
        "vwap_weight": "real summed contract qty per bar",
        "cost_levels_pts": list(COST_LEVELS),
        "oos_start": OOS_START,
        "instrument_note": "signals & fills on TXF itself; 8pt column is the platform TXF→TMF envelope (frozen)",
        "note_volume_profile": "anchored volume profile is a last-bar drawing with no per-bar signal -> not traded",
        "rules": {},
    }

    # accumulate per-rule pooled trades + beta-neutral
    pooled = {r: {"both": [], "long": [], "bn_long": 0.0, "bn_both": 0.0} for r in RULES}
    per_contract_diag = {}
    for c in contracts:
        bars = build_bars(RAW_DIR, c, args.bar_min, session=args.session)
        if len(bars.close) == 0:
            per_contract_diag[c] = {"error": "no bars"}
            continue
        sig = compute_signals(bars.open, bars.high, bars.low, bars.close, bars.volume, bars.date)
        per_contract_diag[c] = {"n_bars": len(bars.close), "n_days": int(np.unique(bars.date).size),
                                "signals": sig.diag}
        for r in RULES:
            both, pa_both = _run_rule(bars, sig, rule=r, side="both")
            longs, pa_long = _run_rule(bars, sig, rule=r, side="long")
            pooled[r]["both"].extend(both)
            pooled[r]["long"].extend(longs)
            bn_l = _beta_neutral(bars, pa_long, side="long", n_trades=len(longs))
            bn_b = _beta_neutral(bars, pa_both, side="both", n_trades=len(both))
            pooled[r]["bn_long"] += bn_l.get("excess_total_pts", 0.0) or 0.0
            pooled[r]["bn_both"] += bn_b.get("excess_total_pts", 0.0) or 0.0
            pooled[r].setdefault("bn_by_contract", {})[c] = {
                "long_only": bn_l, "long_short": bn_b,
            }

    result["per_contract"] = per_contract_diag
    for r in RULES:
        p = pooled[r]
        result["rules"][r] = {
            "long_short": _report_block(p["both"]),
            "long_only": _report_block(p["long"]),
            "beta_neutral_summary": {
                "long_only_excess_total_pts": round(p["bn_long"], 1),
                "long_only_excess_per_trade_pts": round(p["bn_long"] / max(1, len(p["long"])), 2),
                "long_short_excess_total_pts": round(p["bn_both"], 1),
                "long_short_excess_per_trade_pts": round(p["bn_both"] / max(1, len(p["both"])), 2),
            },
            "beta_neutral_by_contract": p.get("bn_by_contract", {}),
        }

    suffix = f"{args.bar_min}m_{args.session}"
    out_path = OUT_DIR / f"of_vwap_backtest_{suffix}.json"
    out_path.write_text(json.dumps(result, indent=2))

    print(f"=== LuxAlgo Order Flow VWAP Deviation | {args.bar_min}m {args.session} bars ===")
    for c, d in per_contract_diag.items():
        if "signals" in d:
            print(f"  {c}: bars={d['n_bars']} days={d['n_days']} signals={d['signals']}")
    for r in RULES:
        blk_ls = result["rules"][r]["long_short"]
        blk_lo = result["rules"][r]["long_only"]
        bn = result["rules"][r]["beta_neutral_summary"]
        print(f"\n--- rule: {r} ---")
        for variant, blk in (("long_short", blk_ls), ("long_only", blk_lo)):
            c8 = blk["by_cost"]["8pt"]["all"]
            o8 = blk["by_cost"]["8pt"]["oos"]
            print(
                f"  [{variant}] trades={blk['n_total']} (IS {blk['n_is']}/OOS {blk['n_oos']}) "
                f"@8pt net_mean={c8.get('net_mean')} med={c8.get('net_median')} wr={c8.get('win_rate')} "
                f"PF={c8.get('profit_factor')} total={c8.get('net_total')} | OOS net_mean={o8.get('net_mean')}"
            )
        print(f"  beta-neutral: long_only excess/trade={bn['long_only_excess_per_trade_pts']} "
              f"long_short excess/trade={bn['long_short_excess_per_trade_pts']}")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
