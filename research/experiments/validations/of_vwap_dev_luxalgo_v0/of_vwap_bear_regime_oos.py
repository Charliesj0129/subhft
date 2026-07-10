"""Bear/high-vol regime OOS check for the vwap_fade long-only rule (BBO fills).

Every prior candidate in this research program was tested inside a single
persistent bull / low-vol tape -- the QA fade result (`of_vwap_qa_backtest.py`,
`_qa_fade_long`) has never been checked against a down-trending or high-vol
window because none was known to exist in the captured data. This driver:

1. Classifies EVERY usable TXF contract-day (txfb6..txfg6 -- all months
   currently on disk, not just the txfd6-txfg6 default) into bear/high-vol
   buckets using only that day's own open->close return and intraday realized
   vol (both available at session close, no lookahead), with data-driven
   (quartile) thresholds plus an explicit genuineness check: the bucket must
   be genuinely negative / genuinely more volatile in absolute terms, not
   just "less bullish" than the rest of a one-sided tape.
2. If a genuine bucket exists, re-runs the frozen `_qa_fade_long` rule
   restricted to trades entered on those contract-days, at the same
   FEE_LEVELS as the baseline QA script, and reports per-bucket metrics
   next to the existing bull-window baseline for direct comparison.

Run:
  uv run python -m research.experiments.validations.of_vwap_dev_luxalgo_v0.of_vwap_bear_regime_oos \
      --bar-min 5 --contracts txfb6,txfc6,txfd6,txfe6,txff6,txfg6 --session day
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from research.experiments.validations.ml_rsi_zeiierman_v0.bars import Bars, build_bars

from .indicator import compute_signals
from .of_vwap_qa_backtest import FEE_LEVELS, _block, _qa_fade_long

RAW_DIR = Path("research/data/raw")
OUT_DIR = Path("research/experiments/validations/of_vwap_dev_luxalgo_v0")
BASELINE_PATH = OUT_DIR / "of_vwap_qa_5m_day.json"

CONTRACTS = ("txfb6", "txfc6", "txfd6", "txfe6", "txff6", "txfg6")  # txfi6 has 1 day, not enough

BEAR_QUANTILE = 0.25  # bottom quartile of daily open->close return
HIGHVOL_QUANTILE = 0.75  # top quartile of intraday realized vol
HIGHVOL_ABS_MULT = 1.25  # top-quartile RV must be >=25% above the median to count as "genuinely" elevated


def _daily_stats(bars: Bars, contract: str) -> list[dict]:
    """One row per session day: open->close return and realized vol, both in points.

    Uses only that day's own bars (available by session close) -- no lookahead.
    """
    rows: list[dict] = []
    n = len(bars.close)
    day_start = 0
    for i in range(n):
        last = bool(bars.is_session_close[i]) or (i == n - 1)
        if not last:
            continue
        date = str(bars.date[day_start])
        o = float(bars.open[day_start])
        c = float(bars.close[i])
        closes = bars.close[day_start : i + 1]
        log_rets = np.diff(np.log(closes))
        rv_frac = float(np.sqrt(np.sum(log_rets**2))) if log_rets.size else 0.0
        rows.append(
            {
                "contract": contract,
                "date": date,
                "open": o,
                "close": c,
                "ret_pts": round(c - o, 1),
                "ret_pct": round((c - o) / o * 100.0, 3),
                "rv_pts": round(rv_frac * o, 1),
                "n_bars": i - day_start + 1,
            }
        )
        day_start = i + 1
    return rows


def _consecutive_negative_runs(rows: list[dict]) -> dict:
    """Diagnostic only: longest run(s) of consecutive negative-return trading days.

    Dedupes same-calendar-date rows across overlapping front/back-month
    contracts by keeping the row with more bars (more reliable estimate);
    ties broken by contract name for determinism.
    """
    by_date: dict[str, dict] = {}
    for r in rows:
        cur = by_date.get(r["date"])
        if cur is None or (r["n_bars"], r["contract"]) > (cur["n_bars"], cur["contract"]):
            by_date[r["date"]] = r
    merged = [by_date[d] for d in sorted(by_date)]

    runs: list[list[str]] = []
    cur_run: list[str] = []
    for r in merged:
        if r["ret_pts"] < 0:
            cur_run.append(r["date"])
        else:
            if len(cur_run) >= 2:
                runs.append(cur_run)
            cur_run = []
    if len(cur_run) >= 2:
        runs.append(cur_run)

    return {
        "n_merged_calendar_days": len(merged),
        "max_consecutive_negative_run": max((len(r) for r in runs), default=0),
        "runs_ge_2": [{"start": r[0], "end": r[-1], "n_days": len(r)} for r in runs],
    }


def _classify_regime(rows: list[dict]) -> dict:
    ret_pts = np.array([r["ret_pts"] for r in rows], dtype=float)
    rv_pts = np.array([r["rv_pts"] for r in rows], dtype=float)

    bear_threshold = float(np.percentile(ret_pts, BEAR_QUANTILE * 100))
    highvol_threshold = float(np.percentile(rv_pts, HIGHVOL_QUANTILE * 100))
    median_rv = float(np.median(rv_pts))

    genuine_bear = bear_threshold < 0.0
    genuine_highvol = median_rv > 0 and (highvol_threshold >= HIGHVOL_ABS_MULT * median_rv)

    bear_rows = [r for r in rows if r["ret_pts"] <= bear_threshold]
    highvol_rows = [r for r in rows if r["rv_pts"] >= highvol_threshold]
    both_rows = [r for r in rows if r["ret_pts"] <= bear_threshold and r["rv_pts"] >= highvol_threshold]

    return {
        "n_contract_days": len(rows),
        "return_pts_dist": {
            "min": round(float(ret_pts.min()), 1),
            "q25": round(bear_threshold, 1),
            "median": round(float(np.median(ret_pts)), 1),
            "q75": round(float(np.percentile(ret_pts, 75)), 1),
            "max": round(float(ret_pts.max()), 1),
            "mean": round(float(ret_pts.mean()), 1),
        },
        "rv_pts_dist": {
            "min": round(float(rv_pts.min()), 1),
            "q25": round(float(np.percentile(rv_pts, 25)), 1),
            "median": round(median_rv, 1),
            "q75": round(highvol_threshold, 1),
            "max": round(float(rv_pts.max()), 1),
            "mean": round(float(rv_pts.mean()), 1),
        },
        "n_negative_return_days": int((ret_pts < 0).sum()),
        "bear_definition": f"ret_pts <= {bear_threshold:.1f} (bottom {BEAR_QUANTILE:.0%} of pooled daily returns)",
        "highvol_definition": (
            f"rv_pts >= {highvol_threshold:.1f} (top {1 - HIGHVOL_QUANTILE:.0%} of pooled realized vol)"
        ),
        "genuine_bear_regime_found": genuine_bear,
        "genuine_highvol_regime_found": genuine_highvol,
        "genuineness_note": (
            "bear bucket counts as genuine only if its threshold is an actually-negative "
            "return (not merely less-bullish); high-vol bucket counts as genuine only if "
            f"its threshold RV is >= {HIGHVOL_ABS_MULT}x the pooled median RV"
        ),
        "bear_rows": bear_rows,
        "highvol_rows": highvol_rows,
        "bear_and_highvol_rows": both_rows,
        "consecutive_negative_runs": _consecutive_negative_runs(rows),
    }


def _dates_by_contract(rows: list[dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r["contract"], set()).add(r["date"])
    return out


def _pooled_subset(trades_by_contract: dict[str, list[dict]], dates_by_contract: dict[str, set[str]]) -> list[dict]:
    """Trades from `trades_by_contract` restricted to each contract's own regime date set."""
    pooled: list[dict] = []
    for c, trades in trades_by_contract.items():
        wanted = dates_by_contract.get(c, set())
        pooled.extend(t for t in trades if t["date"] in wanted)
    return pooled


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bar-min", type=int, default=5)
    ap.add_argument("--contracts", type=str, default=",".join(CONTRACTS))
    ap.add_argument("--session", type=str, default="day", choices=["day", "night"])
    args = ap.parse_args()

    contracts = [c.strip() for c in args.contracts.split(",") if c.strip()]

    all_rows: list[dict] = []
    trades_by_contract: dict[str, list[dict]] = {}
    per_contract_diag: dict[str, dict] = {}

    for c in contracts:
        bars = build_bars(RAW_DIR, c, args.bar_min, session=args.session)
        if len(bars.close) == 0:
            per_contract_diag[c] = {"error": "no bars"}
            continue
        rows = _daily_stats(bars, c)
        all_rows.extend(rows)
        sig = compute_signals(bars.open, bars.high, bars.low, bars.close, bars.volume, bars.date)
        trades_by_contract[c] = _qa_fade_long(bars, sig)
        per_contract_diag[c] = {"n_days": len(rows), "n_trades": len(trades_by_contract[c])}

    if not all_rows:
        result = {
            "schema": "research.of_vwap_bear_regime_oos.v1",
            "finding": "NO_USABLE_DATA",
            "per_contract": per_contract_diag,
        }
        out_path = OUT_DIR / f"of_vwap_bear_regime_oos_{args.bar_min}m_{args.session}.json"
        out_path.write_text(json.dumps(result, indent=2))
        print("No usable contract-days found across", contracts)
        print(f"wrote {out_path}")
        return 0

    regime = _classify_regime(all_rows)
    bear_dates = _dates_by_contract(regime["bear_rows"])
    highvol_dates = _dates_by_contract(regime["highvol_rows"])
    both_dates = _dates_by_contract(regime["bear_and_highvol_rows"])

    bear_block = _block(_pooled_subset(trades_by_contract, bear_dates))
    highvol_block = _block(_pooled_subset(trades_by_contract, highvol_dates))
    both_block = _block(_pooled_subset(trades_by_contract, both_dates))

    baseline_pooled = None
    if BASELINE_PATH.exists():
        baseline_pooled = json.loads(BASELINE_PATH.read_text()).get("pooled")

    result = {
        "schema": "research.of_vwap_bear_regime_oos.v1",
        "rule": "vwap_fade long-only, BBO quote-aware fills (buy=ask, sell=bid) -- verbatim from of_vwap_qa_backtest",
        "bar_min": args.bar_min,
        "session": args.session,
        "contracts_scanned": contracts,
        "fee_levels_on_top_of_spread_pts": list(FEE_LEVELS),
        "per_contract": per_contract_diag,
        "regime_classification": regime,
        "vwap_fade_bear_subset": bear_block,
        "vwap_fade_highvol_subset": highvol_block,
        "vwap_fade_bear_and_highvol_subset": both_block,
        "baseline_bull_window_pooled": {
            "source": str(BASELINE_PATH),
            "pooled": baseline_pooled,
        },
    }

    out_path = OUT_DIR / f"of_vwap_bear_regime_oos_{args.bar_min}m_{args.session}.json"
    out_path.write_text(json.dumps(result, indent=2))

    print(f"=== vwap_fade bear/high-vol regime OOS check | {args.bar_min}m {args.session} ===\n")
    print(f"contract-days classified: {regime['n_contract_days']} across {contracts}")
    print(f"  return_pts: {regime['return_pts_dist']}")
    print(f"  rv_pts:     {regime['rv_pts_dist']}")
    print(f"  bear def: {regime['bear_definition']} -> genuine={regime['genuine_bear_regime_found']}")
    print(f"  highvol def: {regime['highvol_definition']} -> genuine={regime['genuine_highvol_regime_found']}")
    print(f"  consecutive negative runs (merged calendar): {regime['consecutive_negative_runs']}")
    print()
    for label, block in (("BEAR", bear_block), ("HIGH-VOL", highvol_block), ("BEAR+HIGHVOL", both_block)):
        s13 = block["by_fee"]["1.3pt"]["all"]
        print(
            f"  [{label}] n={block['n_total']} (IS {block['n_is']}/OOS {block['n_oos']}) "
            f"+1.3pt fee net_mean={s13.get('net_mean')} med={s13.get('net_median')} "
            f"wr={s13.get('win_rate')} PF={s13.get('profit_factor')} total={s13.get('net_total')}"
        )
    if baseline_pooled:
        b13 = baseline_pooled["by_fee"]["1.3pt"]["all"]
        print(
            f"  [BASELINE bull-window pooled] n={baseline_pooled['n_total']} "
            f"+1.3pt fee net_mean={b13.get('net_mean')} PF={b13.get('profit_factor')}"
        )
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
