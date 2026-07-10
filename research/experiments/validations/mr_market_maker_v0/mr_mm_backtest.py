"""Driver: faithful backtest of 'Mr. Market Maker' (CHoCH + Fib-zone) on TXF bars.

The indicator defines no entries; its one mechanical setup is the EMA200-filtered
CHoCH followed by a close back into the 0.382-0.618 Fib zone of the impulse leg.
We trade exactly that, no-lookahead (entry confirmed at bar close -> fill next bar
open) with the script's own structural bracket: protective stop at the broken
swing (its "Stop Loss level" label) and target at the breakout extreme (the
impulse trigger), plus intraday force-flat at session close.

Honest scoring at 0/2/8pt, IS/OOS split, long-short and long-only, plus the
beta-neutral permutation test that isolates timing alpha from market drift.

Run:
  uv run python -m research.experiments.validations.mr_market_maker_v0.mr_mm_backtest \
      --bar-min 15 --contracts txfb6,txfc6,txfd6,txfe6,txff6 --session day
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from research.experiments.validations.ml_rsi_zeiierman_v0.bars import build_bars
from research.experiments.validations.ml_rsi_zeiierman_v0.ml_rsi_backtest import (
    OOS_START,
    Trade,
    _beta_neutral,
    _report_block,
)

from .indicator import compute_setups

RAW_DIR = Path("research/data/raw")
OUT_DIR = Path("research/experiments/validations/mr_market_maker_v0")

# Time stop: a CHoCH retracement setup that neither targets nor stops out within
# this many bars is force-closed (in addition to session force-flat). The script
# extends its Gann box gannBarsToRight=20 bars; we reuse that as the dwell cap.
MAX_BARS = 20


def _run_bracket(bars, sig, *, side: str, use_stop: bool):
    """No-lookahead bracket engine for the CHoCH Fib-zone entry.

    Entry: signal confirmed at close[i] -> fill at open[i+1]. Exit: target at the
    breakout extreme, protective stop at the broken swing (only if use_stop), a
    MAX_BARS dwell cap, or session force-flat -- whichever first. If both stop and
    target fall inside the same bar, the stop is assumed hit first (conservative).
    """
    n = len(bars.close)
    trades: list[Trade] = []
    pos_active = np.zeros(n, dtype=int)
    pos = 0
    entry_px = 0.0
    entry_dir = 0
    entry_bar = 0
    tp = np.nan
    sl = np.nan

    def close_trade(exit_px: float, date: str) -> None:
        nonlocal pos, entry_dir
        gross = (exit_px - entry_px) if entry_dir == 1 else (entry_px - exit_px)
        trades.append(Trade(dir=entry_dir, gross=float(gross), date=date, oos=date >= OOS_START))
        pos = 0
        entry_dir = 0

    for i in range(n):
        pos_active[i] = pos
        last = bool(bars.is_session_close[i]) or (i == n - 1)

        if pos != 0:
            if last:
                close_trade(bars.close[i], bars.date[i])
            else:
                hi, lo = bars.high[i], bars.low[i]
                if pos == 1:
                    hit_sl = use_stop and (lo <= sl)
                    hit_tp = hi >= tp
                else:  # short
                    hit_sl = use_stop and (hi >= sl)
                    hit_tp = lo <= tp
                if hit_sl:  # conservative: stop assumed first when both in-bar
                    close_trade(sl, bars.date[i])
                elif hit_tp:
                    close_trade(tp, bars.date[i])
                elif (i - entry_bar) >= MAX_BARS:
                    close_trade(bars.close[i], bars.date[i])

        if pos == 0 and not last:
            nxt = i + 1
            fill = bars.open[nxt]
            el, es = bool(sig.enter_long[i]), bool(sig.enter_short[i])
            if el and es:
                continue  # contradictory same-bar -> stand aside
            if el and side in ("both", "long"):
                pos, entry_dir, entry_px, entry_bar = 1, 1, fill, nxt
                tp, sl = sig.target_long[i], sig.stop_long[i]
            elif es and side == "both":
                pos, entry_dir, entry_px, entry_bar = -1, -1, fill, nxt
                tp, sl = sig.target_short[i], sig.stop_short[i]

    return trades, pos_active


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar-min", type=int, default=15)
    ap.add_argument("--contracts", type=str, default="txfb6,txfc6,txfd6,txfe6,txff6")
    ap.add_argument("--session", type=str, default="day", choices=["day", "night"])
    args = ap.parse_args()

    contracts = [c.strip() for c in args.contracts.split(",") if c.strip()]
    result = {
        "schema": "research.mr_market_maker_backtest.v1",
        "strategy": "Mr. Market Maker -- EMA200-filtered CHoCH + 0.382-0.618 Fib-zone entry, no tuning",
        "bar_min": args.bar_min,
        "contracts": contracts,
        "session": f"TAIFEX {args.session} session, intraday force-flat",
        "execution": "CHoCH+Fib-zone entry at confirmed close -> fill next bar open; force-flat at session close",
        "bracket": f"target=breakout extreme; stop=broken swing (stop variant); dwell cap={MAX_BARS} bars",
        "oos_start": OOS_START,
        "instrument_note": "signals & fills on TXF itself; 8pt column is the platform TXF→TMF envelope (frozen)",
        "panel_note": "indicator decoration/labels are display-only; only the mechanical CHoCH+Fib entry is traded",
        "variants": {},
    }

    variants = {"with_protective_stop": True, "target_time_only": False}
    pooled = {v: {"both": [], "long": [], "bn_long": 0.0, "bn_both": 0.0, "bn_by_contract": {}}
              for v in variants}
    per_contract_diag = {}

    for c in contracts:
        bars = build_bars(RAW_DIR, c, args.bar_min, session=args.session)
        if len(bars.close) == 0:
            per_contract_diag[c] = {"error": "no bars"}
            continue
        sig = compute_setups(bars.open, bars.high, bars.low, bars.close, bars.date)
        per_contract_diag[c] = {"n_bars": len(bars.close), "n_days": int(np.unique(bars.date).size),
                                "events": sig.diag}
        for v, use_stop in variants.items():
            both, pa_both = _run_bracket(bars, sig, side="both", use_stop=use_stop)
            longs, pa_long = _run_bracket(bars, sig, side="long", use_stop=use_stop)
            pooled[v]["both"].extend(both)
            pooled[v]["long"].extend(longs)
            bn_l = _beta_neutral(bars, pa_long, side="long", n_trades=len(longs))
            bn_b = _beta_neutral(bars, pa_both, side="both", n_trades=len(both))
            pooled[v]["bn_long"] += bn_l.get("excess_total_pts", 0.0) or 0.0
            pooled[v]["bn_both"] += bn_b.get("excess_total_pts", 0.0) or 0.0
            pooled[v]["bn_by_contract"][c] = {"long_only": bn_l, "long_short": bn_b}

    result["per_contract"] = per_contract_diag
    for v in variants:
        p = pooled[v]
        result["variants"][v] = {
            "long_short": _report_block(p["both"]),
            "long_only": _report_block(p["long"]),
            "beta_neutral_summary": {
                "long_only_excess_total_pts": round(p["bn_long"], 1),
                "long_only_excess_per_trade_pts": round(p["bn_long"] / max(1, len(p["long"])), 2),
                "long_short_excess_total_pts": round(p["bn_both"], 1),
                "long_short_excess_per_trade_pts": round(p["bn_both"] / max(1, len(p["both"])), 2),
            },
            "beta_neutral_by_contract": p["bn_by_contract"],
        }

    suffix = f"{args.bar_min}m_{args.session}"
    out_path = OUT_DIR / f"mr_mm_backtest_{suffix}.json"
    out_path.write_text(json.dumps(result, indent=2))

    print(f"=== Mr. Market Maker (CHoCH+Fib) | {args.bar_min}m {args.session} bars ===")
    for c, dg in per_contract_diag.items():
        if "events" in dg:
            e = dg["events"]
            print(f"  {c}: bars={dg['n_bars']} days={dg['n_days']} "
                  f"choch(bull/bear)={e['choch_bull']}/{e['choch_bear']} "
                  f"entries(L/S)={e['entries_long']}/{e['entries_short']}")
    for v in variants:
        print(f"\n--- variant: {v} ---")
        for variant in ("long_short", "long_only"):
            blk = result["variants"][v][variant]
            bn = result["variants"][v]["beta_neutral_summary"]
            for ck in ("0pt", "2pt", "8pt"):
                a = blk["by_cost"][ck]["all"]
                o = blk["by_cost"][ck]["oos"]
                print(f"  [{variant} {ck:>3}] trades={blk['n_total']} (IS {blk['n_is']}/OOS {blk['n_oos']}) "
                      f"net_mean={a.get('net_mean')} med={a.get('net_median')} wr={a.get('win_rate')} "
                      f"PF={a.get('profit_factor')} total={a.get('net_total')} | OOS net_mean={o.get('net_mean')}")
            bn_key = "long_only_excess_per_trade_pts" if variant == "long_only" else "long_short_excess_per_trade_pts"
            print(f"  [{variant}] beta-neutral excess/trade = {bn[bn_key]} pts")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
