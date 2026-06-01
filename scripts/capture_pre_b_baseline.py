"""Slice B task 1 — Pre-B baseline capture (DoD-B1 evidence).

Runs canonical R47 backtest under current main MakerEngine on 31-day TMFD6
fixture with `r47_maker_shioaji_p95_v2026-04-24_measured` and persists the
artifact at tests/fixtures/maker_engine_pre_mtm_baseline/r47_tmfd6_31d_pre_b.json.

Strategy: TmfD6SoloMakerMinimal(C60Params()) — deployed-config proxy used by
scripts/compare_r47_latency.py (credibility audit doc line 535: 39 fills,
+2,398 NTD, Sharpe +2.80).

Invocation:
    CLICKHOUSE_PASSWORD=$(grep CLICKHOUSE_PASSWORD /home/charlie/hft_platform/.env | cut -d= -f2) \\
        uv run python scripts/capture_pre_b_baseline.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

from hft_platform.alpha.latency_profiles import resolve_profile
from research.alphas.c60_tmfd6_r47_minimal_inst_rt.impl import (
    C60Params,
    TmfD6SoloMakerMinimal,
)
from research.backtest.cost_models import load_cost_profile
from research.backtest.fill_models import QueueDepletionFill
from research.backtest.maker_engine import ClickHouseSource, LatencyProfile, MakerEngine

_INSTRUMENT = "TMFD6"
_LATENCY_PROFILE = "r47_maker_shioaji_p95_v2026-04-24_measured"
_POINT_VALUE_NTD = 10
_FIXTURE_ID = "tmfd6_31d_2026_03_to_2026_04"
_EXPECTED_PNL_NTD = 2_398.0
_EXPECTED_FILLS = 39
_PNL_TOLERANCE_NTD = 200.0


def main() -> int:
    if not os.environ.get("CLICKHOUSE_PASSWORD"):
        print("ERROR: CLICKHOUSE_PASSWORD is not set.", file=sys.stderr)
        return 2

    profile = resolve_profile(_LATENCY_PROFILE)
    latency = LatencyProfile(
        place_ns=int(float(profile["submit_ack_latency_ms"]) * 1_000_000),
        cancel_ns=int(float(profile["cancel_ack_latency_ms"]) * 1_000_000),
    )
    engine = MakerEngine(
        fill_model=QueueDepletionFill(queue_fraction=0.5),
        cost_model=load_cost_profile(_INSTRUMENT),
        ck_source=ClickHouseSource(),
        latency_profile=latency,
    )
    strategy = TmfD6SoloMakerMinimal(params=C60Params(), active_symbol=_INSTRUMENT)
    result = engine.run(strategy=strategy, instrument=_INSTRUMENT, pipeline_mode="strict")

    sc = result.maker_scorecard or {}
    total_pnl_pts = float(sc.get("total_pnl_pts", 0.0))
    total_fills = int(sc.get("total_fills", 0))
    pnl_ntd = round(total_pnl_pts * _POINT_VALUE_NTD, 2)

    artifact = {
        "pnl_ntd": pnl_ntd,
        "pnl_pts": round(total_pnl_pts, 4),
        "fills": total_fills,
        "winning_days": sc.get("winning_days", 0),
        "n_days": sc.get("n_days", 0),
        "sharpe_is": round(float(result.sharpe_is), 4),
        "max_drawdown": round(float(result.max_drawdown), 6),
        "daily_pnl": result.daily_pnl or [],
        "fixture_id": _FIXTURE_ID,
        "instrument": _INSTRUMENT,
        "latency_profile": _LATENCY_PROFILE,
        "latency_place_ms": float(profile["submit_ack_latency_ms"]),
        "latency_cancel_ms": float(profile["cancel_ack_latency_ms"]),
        "queue_fraction": 0.5,
        "strategy": "TmfD6SoloMakerMinimal(C60Params())",
        "captured_at": date.today().isoformat(),
        "captured_by": "slice-b-task-1",
    }
    out = Path("tests/fixtures/maker_engine_pre_mtm_baseline/r47_tmfd6_31d_pre_b.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, sort_keys=True, indent=2) + "\n")
    print(f"Wrote {out}: pnl_ntd={pnl_ntd}, fills={total_fills}, sharpe={artifact['sharpe_is']}")

    if total_fills != _EXPECTED_FILLS or abs(pnl_ntd - _EXPECTED_PNL_NTD) > _PNL_TOLERANCE_NTD:
        print(
            f"WARN: deviation — expected +{_EXPECTED_PNL_NTD:.0f} NTD / "
            f"{_EXPECTED_FILLS} fills, got {pnl_ntd:.2f} / {total_fills}.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
