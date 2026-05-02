"""P0 follow-up — sweep C60 spread_threshold_pts under v2026-04-24 profile.

Question: under measured live-broker RTT (210 ms), is there a spread_threshold
setting that pulls C60 PnL back to positive?

Sweep: spread_threshold_pts ∈ {5, 7, 10, 15}; holds all other C60Params
at deployed defaults (max_pos=1, inventory_skew_tenths=2, QI layer on).

Invocation:
    CLICKHOUSE_PASSWORD=$(grep CLICKHOUSE_PASSWORD .env | cut -d= -f2) \\
        PYTHONPATH=. uv run python scripts/sweep_r47_spread.py
"""

from __future__ import annotations

import json
import os
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


def _resolve_bridge(name: str) -> LatencyProfile:
    p = resolve_profile(name)
    return LatencyProfile(
        place_ns=int(float(p["submit_ack_latency_ms"]) * 1_000_000),
        cancel_ns=int(float(p["cancel_ack_latency_ms"]) * 1_000_000),
    )


def _run_one(spread_threshold_pts: int, latency: LatencyProfile) -> dict:
    ck = ClickHouseSource()
    cost = load_cost_profile(_INSTRUMENT)
    fill = QueueDepletionFill(queue_fraction=0.5)
    engine = MakerEngine(
        fill_model=fill,
        cost_model=cost,
        ck_source=ck,
        latency_profile=latency,
    )
    params = C60Params(spread_threshold_pts=spread_threshold_pts)
    strategy = TmfD6SoloMakerMinimal(params=params, active_symbol=_INSTRUMENT)
    result = engine.run(strategy=strategy, instrument=_INSTRUMENT, pipeline_mode="strict")
    sc = result.maker_scorecard or {}
    pnl_pts = float(sc.get("total_pnl_pts", 0.0))
    fills = int(sc.get("total_fills", 0))
    return {
        "spread_threshold_pts": spread_threshold_pts,
        "fills": fills,
        "pnl_pts": round(pnl_pts, 2),
        "pnl_ntd": round(pnl_pts * _POINT_VALUE_NTD, 2),
        "pnl_per_fill_pts": round(pnl_pts / fills if fills else 0.0, 4),
        "winning_days": sc.get("winning_days", 0),
        "n_days": sc.get("n_days", 0),
        "sharpe": round(result.sharpe_is, 3),
    }


def main() -> int:
    if not os.environ.get("CLICKHOUSE_PASSWORD"):
        print("ERROR: CLICKHOUSE_PASSWORD not set", file=sys.stderr)
        return 2

    latency = _resolve_bridge(_PROFILE_NAME)
    thresholds = [5, 7, 10, 15]
    rows = [_run_one(t, latency) for t in thresholds]

    print(f"\n=== C60 TmfD6SoloMakerMinimal spread sweep under {_PROFILE_NAME} ===")
    print(
        f"(place={latency.place_ns / 1_000_000:.0f} ms, "
        f"cancel={latency.cancel_ns / 1_000_000:.0f} ms)\n"
    )
    print(
        f"{'spread_thr_pts':>16}{'fills':>8}"
        f"{'pnl_pts':>12}{'pnl_ntd':>12}"
        f"{'pnl/fill':>12}{'win_days':>10}{'sharpe':>8}"
    )
    for r in rows:
        print(
            f"{r['spread_threshold_pts']:>16}"
            f"{r['fills']:>8}"
            f"{r['pnl_pts']:>12.2f}"
            f"{r['pnl_ntd']:>12.2f}"
            f"{r['pnl_per_fill_pts']:>12.4f}"
            f"{r['winning_days']:>10}"
            f"{r['sharpe']:>8.3f}"
        )
    print()
    print("JSON:")
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
