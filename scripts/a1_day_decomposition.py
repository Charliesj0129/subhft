"""A1 — day-level PnL decomposition for R47 +2,398 NTD result.

Runs C60 TmfD6SoloMakerMinimal on TMFD6 under the measured profile
(r47_maker_shioaji_p95_v2026-04-24_measured; place=395ms cancel=59ms)
and emits per-day decomposition + §4/§6/jackknife statistics.

Reference: roadmap Bucket A1. Answer tree:
  DIFFUSE-SURVIVE  : max_day <=25% AND winning_days >=5 AND no jackknife sign-flip
  OUTLIER-KILL     : 1-2 days contribute >50% AND jackknife flips sign
  EDGE/MARGINAL    : partial pass

Invocation:
    CLICKHOUSE_PASSWORD=$(grep CLICKHOUSE_PASSWORD .env | cut -d= -f2) \\
        PYTHONPATH=. uv run python scripts/a1_day_decomposition.py
"""

from __future__ import annotations

import json
import os
import random
import statistics
import sys

from hft_platform.alpha.latency_profiles import resolve_profile
from research.alphas.c60_tmfd6_r47_minimal_inst_rt.impl import (
    C60Params,
    TmfD6SoloMakerMinimal,
)
from research.backtest.cost_models import load_cost_profile
from research.backtest.fill_models import QueueDepletionFill
from research.backtest.maker_engine import (
    ClickHouseSource,
    LatencyProfile,
    MakerEngine,
)

_INSTRUMENT = "TMFD6"
_POINT_VALUE_NTD = 10
_PROFILE_NAME = "r47_maker_shioaji_p95_v2026-04-24_measured"
_BOOTSTRAP_ITERS = 10000
_BOOTSTRAP_SEED = 20260424


def _resolve_bridge(name: str) -> LatencyProfile:
    p = resolve_profile(name)
    return LatencyProfile(
        place_ns=int(float(p["submit_ack_latency_ms"]) * 1_000_000),
        cancel_ns=int(float(p["cancel_ack_latency_ms"]) * 1_000_000),
    )


def _run_engine() -> dict:
    ck = ClickHouseSource()
    cost = load_cost_profile(_INSTRUMENT)
    fill = QueueDepletionFill(queue_fraction=0.5)
    latency = _resolve_bridge(_PROFILE_NAME)
    engine = MakerEngine(
        fill_model=fill,
        cost_model=cost,
        ck_source=ck,
        latency_profile=latency,
    )
    strategy = TmfD6SoloMakerMinimal(params=C60Params(), active_symbol=_INSTRUMENT)
    result = engine.run(strategy=strategy, instrument=_INSTRUMENT, pipeline_mode="strict")
    return {
        "daily_pnl": result.daily_pnl,
        "scorecard": result.maker_scorecard,
        "sharpe_is": result.sharpe_is,
        "data_period": result.data_period,
        "latency_ms": (latency.place_ns / 1e6, latency.cancel_ns / 1e6),
    }


def _decompose(daily: list[dict]) -> dict:
    rows_sorted_abs = sorted(daily, key=lambda d: abs(d["pnl_pts"]), reverse=True)
    rows_sorted_signed = sorted(daily, key=lambda d: d["pnl_pts"], reverse=True)

    pnls_pts = [d["pnl_pts"] for d in daily]
    total_pts = sum(pnls_pts)
    total_abs_pts = sum(abs(p) for p in pnls_pts)

    n_days = len(daily)
    winning_days = sum(1 for p in pnls_pts if p > 0)
    losing_days = sum(1 for p in pnls_pts if p < 0)
    flat_days = n_days - winning_days - losing_days

    max_day_abs = max(abs(p) for p in pnls_pts) if pnls_pts else 0.0
    max_day_signed = max(pnls_pts) if pnls_pts else 0.0
    min_day_signed = min(pnls_pts) if pnls_pts else 0.0

    max_day_abs_share = max_day_abs / total_abs_pts if total_abs_pts > 0 else 0
    max_day_signed_share = max_day_signed / total_pts if abs(total_pts) > 1e-9 else 0

    # Jackknife: total PnL excluding each day
    jackknife = []
    for i, d in enumerate(daily):
        without = total_pts - d["pnl_pts"]
        jackknife.append(
            {
                "date": d["date"],
                "day_pnl_pts": d["pnl_pts"],
                "total_without_day_pts": round(without, 2),
                "total_without_day_ntd": round(without * _POINT_VALUE_NTD, 2),
                "sign_flip": (total_pts > 0) != (without > 0),
            }
        )
    sign_flips = [j for j in jackknife if j["sign_flip"]]

    # Top-N contribution
    top1_abs = rows_sorted_abs[0]["pnl_pts"] if rows_sorted_abs else 0
    top2_abs_sum = sum(abs(r["pnl_pts"]) for r in rows_sorted_abs[:2])
    top3_abs_sum = sum(abs(r["pnl_pts"]) for r in rows_sorted_abs[:3])

    # Bootstrap CI on mean daily PnL
    rng = random.Random(_BOOTSTRAP_SEED)
    bootstrap_means = []
    for _ in range(_BOOTSTRAP_ITERS):
        sample = [rng.choice(pnls_pts) for _ in range(n_days)]
        bootstrap_means.append(sum(sample) / n_days)
    bootstrap_means.sort()
    lo_idx = int(0.025 * _BOOTSTRAP_ITERS)
    hi_idx = int(0.975 * _BOOTSTRAP_ITERS)
    ci95_lo = bootstrap_means[lo_idx]
    ci95_hi = bootstrap_means[hi_idx]
    frac_bootstrap_negative = sum(1 for m in bootstrap_means if m <= 0) / _BOOTSTRAP_ITERS

    mean_daily = sum(pnls_pts) / n_days if n_days else 0
    std_daily = statistics.pstdev(pnls_pts) if n_days > 1 else 0
    median_daily = statistics.median(pnls_pts) if pnls_pts else 0

    return {
        "n_days": n_days,
        "total_pnl_pts": round(total_pts, 2),
        "total_pnl_ntd": round(total_pts * _POINT_VALUE_NTD, 2),
        "total_abs_pnl_pts": round(total_abs_pts, 2),
        "winning_days": winning_days,
        "losing_days": losing_days,
        "flat_days": flat_days,
        "max_day_abs_share": round(max_day_abs_share, 4),
        "max_day_signed_share": round(max_day_signed_share, 4),
        "max_day_pnl_pts": round(max_day_signed, 2),
        "min_day_pnl_pts": round(min_day_signed, 2),
        "mean_daily_pnl_pts": round(mean_daily, 4),
        "std_daily_pnl_pts": round(std_daily, 4),
        "median_daily_pnl_pts": round(median_daily, 4),
        "top1_abs_share": round(abs(top1_abs) / total_abs_pts if total_abs_pts > 0 else 0, 4),
        "top2_abs_share": round(top2_abs_sum / total_abs_pts if total_abs_pts > 0 else 0, 4),
        "top3_abs_share": round(top3_abs_sum / total_abs_pts if total_abs_pts > 0 else 0, 4),
        "rows_sorted_abs_desc": rows_sorted_abs,
        "rows_sorted_signed_desc": rows_sorted_signed,
        "jackknife": jackknife,
        "n_jackknife_sign_flips": len(sign_flips),
        "jackknife_sign_flip_dates": [j["date"] for j in sign_flips],
        "bootstrap_mean_daily_ci95_pts": (round(ci95_lo, 4), round(ci95_hi, 4)),
        "bootstrap_mean_daily_ci95_ntd": (
            round(ci95_lo * _POINT_VALUE_NTD, 2),
            round(ci95_hi * _POINT_VALUE_NTD, 2),
        ),
        "bootstrap_frac_negative_mean": round(frac_bootstrap_negative, 4),
    }


def _verdict(d: dict) -> tuple[str, list[str]]:
    """Decision tree per task description."""
    notes = []
    max_share = d["max_day_abs_share"]
    win_days = d["winning_days"]
    sign_flips = d["n_jackknife_sign_flips"]
    top2_share = d["top2_abs_share"]

    # DIFFUSE-SURVIVE: max_day <= 25% AND winning_days >= 5 AND no jackknife sign-flip
    diffuse = max_share <= 0.25 and win_days >= 5 and sign_flips == 0

    # OUTLIER-KILL: 1-2 days contribute >50% AND jackknife flips sign when excluded
    outlier = top2_share > 0.50 and sign_flips >= 1

    if diffuse:
        verdict = "DIFFUSE-SURVIVE"
        notes.append(f"max_day_abs_share={max_share:.1%} <= 25% (§4 pass)")
        notes.append(f"winning_days={win_days} >= 5 (§6 pass)")
        notes.append(f"jackknife sign-flips={sign_flips} (robust to single-day removal)")
    elif outlier:
        verdict = "OUTLIER-KILL"
        notes.append(f"top-2 absolute share={top2_share:.1%} > 50% (outlier-dominated)")
        notes.append(f"jackknife sign-flips={sign_flips} (sign not robust to single-day removal)")
        notes.append(f"winning_days={win_days} (single / sparse winning-day pattern)")
    else:
        verdict = "EDGE/MARGINAL"
        if max_share > 0.25:
            notes.append(f"max_day_abs_share={max_share:.1%} > 25% (§4 WARN)")
        if win_days < 5:
            notes.append(f"winning_days={win_days} < 5 (§6 WARN)")
        if sign_flips > 0:
            notes.append(f"jackknife sign-flips={sign_flips} (sign not robust)")
        if top2_share > 0.50:
            notes.append(f"top-2 absolute share={top2_share:.1%} > 50% (concentrated)")
        notes.append("Partial pass / partial fail — requires A2 expansion (+8 remote days)")
    return verdict, notes


def main() -> int:
    if not os.environ.get("CLICKHOUSE_PASSWORD"):
        print("ERROR: CLICKHOUSE_PASSWORD is not set", file=sys.stderr)
        return 2

    print(f"Running {_INSTRUMENT} R47-minimal under {_PROFILE_NAME}...", file=sys.stderr)
    raw = _run_engine()
    daily = raw["daily_pnl"]
    if not daily:
        print("ERROR: engine returned no daily_pnl", file=sys.stderr)
        return 3

    analysis = _decompose(daily)
    verdict, notes = _verdict(analysis)

    out = {
        "run": {
            "instrument": _INSTRUMENT,
            "profile": _PROFILE_NAME,
            "latency_ms_place_cancel": raw["latency_ms"],
            "data_period": raw["data_period"],
            "sharpe_is": raw["sharpe_is"],
            "scorecard": raw["scorecard"],
        },
        "analysis": analysis,
        "verdict": verdict,
        "verdict_notes": notes,
    }

    # Human-readable header to stderr
    print(file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print(f"A1 day decomposition — {_INSTRUMENT} @ {_PROFILE_NAME}", file=sys.stderr)
    print("=" * 72, file=sys.stderr)
    print(f"n_days={analysis['n_days']}, total PnL={analysis['total_pnl_ntd']:.0f} NTD "
          f"({analysis['total_pnl_pts']:.2f} pts), winning_days={analysis['winning_days']}", file=sys.stderr)
    print(f"max_day_abs_share={analysis['max_day_abs_share']:.1%}, "
          f"top2={analysis['top2_abs_share']:.1%}, top3={analysis['top3_abs_share']:.1%}", file=sys.stderr)
    print(f"bootstrap CI95 mean-daily-pts = [{analysis['bootstrap_mean_daily_ci95_pts'][0]:.3f}, "
          f"{analysis['bootstrap_mean_daily_ci95_pts'][1]:.3f}], "
          f"frac_negative_mean={analysis['bootstrap_frac_negative_mean']:.1%}", file=sys.stderr)
    print(f"jackknife sign_flips={analysis['n_jackknife_sign_flips']}", file=sys.stderr)
    print(f"VERDICT: {verdict}", file=sys.stderr)
    for n in notes:
        print(f"  - {n}", file=sys.stderr)
    print(file=sys.stderr)

    # JSON output to stdout (for programmatic consumption)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
