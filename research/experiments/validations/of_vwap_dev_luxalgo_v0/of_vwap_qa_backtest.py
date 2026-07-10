"""Quote-aware (BBO) fill re-validation of the LuxAlgo VWAP-band fade.

The trade-print backtest filled at the next bar's first print. This driver
re-runs the SAME vwap_fade long-only rule but fills realistically against the
recorded best bid/ask: a market BUY pays the ask, the closing SELL hits the bid
-- so the strategy pays the true spread at the 2-sigma extension. The question:
does the edge survive real execution, or was it a mid-price fill artifact?

qa_gross already embeds the spread (both legs). Fees/slippage are then layered
on top at a few levels. Per-contract quote coverage + median spread paid are
reported so illiquid back-months (wide/sparse BBO) are visible, not hidden.

Run:
  uv run python -m research.experiments.validations.of_vwap_dev_luxalgo_v0.of_vwap_qa_backtest \
      --bar-min 5 --contracts txfd6,txfe6,txff6,txfg6 --session day
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import numpy as np

from research.experiments.validations.ml_rsi_zeiierman_v0.bars import build_bars
from research.experiments.validations.ml_rsi_zeiierman_v0.ml_rsi_backtest import OOS_START

from .indicator import compute_signals

RAW_DIR = Path("research/data/raw")
OUT_DIR = Path("research/experiments/validations/of_vwap_dev_luxalgo_v0")
# fee/slippage layered ON TOP of the spread already paid by bid/ask fills (TXF pts, round trip)
FEE_LEVELS = (0.0, 1.3, 3.0)  # 0 = spread only; 1.3 ≈ TXF fee+tax RT; 3 = +slippage buffer


def _qa_fade_long(bars, sig):
    """vwap_fade long-only with BBO fills. Returns list of dicts per trade."""
    n = len(bars.close)
    bid = bars.bid_open
    ask = bars.ask_open
    trades = []
    pos = 0
    entry_ask = 0.0
    entry_spread = 0.0
    entry_date = ""
    for i in range(n):
        last = bool(bars.is_session_close[i]) or (i == n - 1)
        if pos == 1 and not last:
            # target exit: close reverts to VWAP -> SELL at next-bar bid
            if not np.isnan(sig.vwap[i]) and bars.close[i] >= sig.vwap[i]:
                bx = bid[i + 1]
                if not np.isnan(bx):
                    trades.append({"gross": float(bx - entry_ask), "date": entry_date,
                                   "oos": entry_date >= OOS_START, "spread": entry_spread})
                    pos = 0
        if pos == 0 and not last:
            if not np.isnan(sig.vlower[i]) and bars.close[i] <= sig.vlower[i]:
                ax = ask[i + 1]
                bx = bid[i + 1]
                if not np.isnan(ax) and not np.isnan(bx):  # need a real quote to enter
                    pos, entry_ask, entry_date = 1, float(ax), bars.date[i]
                    entry_spread = float(ax - bx)
        if last and pos == 1:
            # forced session-close exit: SELL at best available bid (this bar's open quote)
            bx = bid[i] if not np.isnan(bid[i]) else bars.close[i]
            trades.append({"gross": float(bx - entry_ask), "date": entry_date,
                           "oos": entry_date >= OOS_START, "spread": entry_spread})
            pos = 0
    return trades


def _metrics(trades, fee):
    if not trades:
        return {"n": 0}
    nets = [t["gross"] - fee for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")
    return {
        "n": len(trades),
        "net_mean": round(statistics.mean(nets), 2),
        "net_median": round(statistics.median(nets), 1),
        "net_total": round(float(sum(nets)), 1),
        "win_rate": round(len(wins) / len(trades), 3),
        "profit_factor": round(pf, 3) if pf != float("inf") else None,
    }


def _block(trades):
    is_t = [t for t in trades if not t["oos"]]
    oos_t = [t for t in trades if t["oos"]]
    out = {"n_total": len(trades), "n_is": len(is_t), "n_oos": len(oos_t), "by_fee": {}}
    for f in FEE_LEVELS:
        out["by_fee"][f"{f:g}pt"] = {"all": _metrics(trades, f), "oos": _metrics(oos_t, f)}
    if trades:
        out["median_spread_paid_pts"] = round(statistics.median(t["spread"] for t in trades), 2)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar-min", type=int, default=5)
    ap.add_argument("--contracts", type=str, default="txfd6,txfe6,txff6,txfg6")
    ap.add_argument("--session", type=str, default="day", choices=["day", "night"])
    args = ap.parse_args()

    contracts = [c.strip() for c in args.contracts.split(",") if c.strip()]
    result = {
        "schema": "research.of_vwap_qa_backtest.v1",
        "rule": "vwap_fade long-only, BBO quote-aware fills (buy=ask, sell=bid)",
        "bar_min": args.bar_min,
        "session": args.session,
        "fee_levels_on_top_of_spread_pts": list(FEE_LEVELS),
        "oos_start": OOS_START,
        "per_contract": {},
        "pooled": {},
    }
    pooled = []
    for c in contracts:
        bars = build_bars(RAW_DIR, c, args.bar_min, session=args.session)
        if len(bars.close) == 0:
            result["per_contract"][c] = {"error": "no bars"}
            continue
        sig = compute_signals(bars.open, bars.high, bars.low, bars.close, bars.volume, bars.date)
        quote_cov = float(np.mean(~np.isnan(bars.ask_open)))
        med_spread = float(np.nanmedian(bars.ask_open - bars.bid_open))
        trades = _qa_fade_long(bars, sig)
        pooled.extend(trades)
        result["per_contract"][c] = {
            "bar_quote_coverage": round(quote_cov, 3),
            "median_bar_spread_pts": round(med_spread, 2),
            **_block(trades),
        }
    result["pooled"] = _block(pooled)

    out_path = OUT_DIR / f"of_vwap_qa_{args.bar_min}m_{args.session}.json"
    out_path.write_text(json.dumps(result, indent=2))

    print(f"=== vwap_fade long-only | QUOTE-AWARE (BBO) fills | {args.bar_min}m {args.session} ===")
    print("   (fee layered on top of the spread already paid; spread = real ask-bid at fill)\n")
    for c, d in result["per_contract"].items():
        if "n_total" not in d:
            print(f"  {c}: {d.get('error')}")
            continue
        s0 = d["by_fee"]["0pt"]["all"]
        s13 = d["by_fee"]["1.3pt"]["all"]
        print(f"  {c}: coverage={d['bar_quote_coverage']} med_bar_spread={d['median_bar_spread_pts']}pt "
              f"spread_paid={d.get('median_spread_paid_pts')}pt | trades={d['n_total']} "
              f"net(spread-only)={s0['net_mean']} PF={s0['profit_factor']} | net(+1.3 fee)={s13['net_mean']} "
              f"PF={s13['profit_factor']} OOS={d['by_fee']['1.3pt']['oos'].get('net_mean')}")
    p = result["pooled"]
    print(f"\n  POOLED trades={p['n_total']} (IS {p['n_is']}/OOS {p['n_oos']}) "
          f"spread_paid={p.get('median_spread_paid_pts')}pt")
    for f in FEE_LEVELS:
        cell = p["by_fee"][f"{f:g}pt"]["all"]
        oos = p["by_fee"][f"{f:g}pt"]["oos"]
        print(f"    +{f:g}pt fee: net_mean={cell['net_mean']} med={cell['net_median']} wr={cell['win_rate']} "
              f"PF={cell['profit_factor']} total={cell['net_total']} | OOS net_mean={oos.get('net_mean')}")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
