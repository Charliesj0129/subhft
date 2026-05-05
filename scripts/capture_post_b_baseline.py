"""Slice B task 15 — Post-B baseline capture (DoD-B1 evidence).

Runs the canonical R47 backtest under the **post-Slice-B** ``MakerEngine``
(MtM-aware day-end residual; ``QueueDepletionFill`` consuming the calibrated
``QHatTable`` for TMFD6) on the same 31-day TMFD6 fixture and same
``r47_maker_shioaji_p95_v2026-04-24_measured`` profile used by Task 1, and
persists the artifact at
``tests/fixtures/maker_engine_pre_mtm_baseline/r47_tmfd6_31d_post_b.json``.

Comparison anchor: Task 1's ``r47_tmfd6_31d_pre_b.json`` recorded
``pnl_ntd=+2398.0, fills=39`` under the pre-Slice-B engine. DoD-B1 expects the
post-B PnL to fall to or below the maker cost floor
(``cost_floor_per_fill_pts=0.5`` × 10 NTD/pt × 39 fills = 195 NTD).

Clock-handling note
-------------------
``MakerEngine`` does not yet pass an event-driven clock into ``QueueDepletionFill``.
The default clock returns 0 ns (-> hour=0 lookup), which means q_hat is sourced
from the ``hour=0`` cells of the calibrated table for every event. For TMFD6
the hour=0 cells are ``shallow=0.5054`` and ``deep=0.3827`` -- close to the
legacy literal ``0.5``, so the dominant Slice B effect on PnL is the day-end
MtM residual rather than the queue-fraction shift. Wiring a proper event clock
is tracked in Task 16 / out of scope here.

Invocation::

    CLICKHOUSE_PASSWORD=$(grep CLICKHOUSE_PASSWORD /home/charlie/hft_platform/.env | cut -d= -f2) \\
        uv run python scripts/capture_post_b_baseline.py
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
from research.backtest.q_hat_table import QHatTable

_INSTRUMENT = "TMFD6"
_LATENCY_PROFILE = "r47_maker_shioaji_p95_v2026-04-24_measured"
_POINT_VALUE_NTD = 10
_FIXTURE_ID = "tmfd6_31d_2026_03_to_2026_04"
_QHAT_PATH = Path("research/backtest/q_hat_data/tmfd6_q_hat.parquet")
# DoD-B1 threshold derivation:
#   cost_floor_per_fill_pts (vm_ul6_strict.yaml) = 0.5
#   point_value_ntd (TMFD6, mini-TAIEX) = 10
#   pre-B fills = 39 (invariant under MtM bookkeeping; DoD-B1 will assert it
#   stays at 39 under post-B too).
_COST_FLOOR_PER_FILL_NTD = 5.0  # 0.5 pts * 10 NTD/pt
_EXPECTED_FILLS = 39


def main() -> int:
    if not os.environ.get("CLICKHOUSE_PASSWORD"):
        print("ERROR: CLICKHOUSE_PASSWORD is not set.", file=sys.stderr)
        return 2

    if not _QHAT_PATH.exists():
        print(f"ERROR: q_hat fixture missing at {_QHAT_PATH}", file=sys.stderr)
        return 2

    profile = resolve_profile(_LATENCY_PROFILE)
    latency = LatencyProfile(
        place_ns=int(float(profile["submit_ack_latency_ms"]) * 1_000_000),
        cancel_ns=int(float(profile["cancel_ack_latency_ms"]) * 1_000_000),
    )
    qhat_table = QHatTable.load(_QHAT_PATH)
    fill_model = QueueDepletionFill(
        queue_fraction=0.5,
        q_hat_table=qhat_table,
        symbol=_INSTRUMENT,
    )
    engine = MakerEngine(
        fill_model=fill_model,
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

    # Slice A blocking gates (e.g. day_bootstrap_ci) iterate equity_curve, so
    # serialise the actual numpy array (rather than the legacy zeros(1) sentinel
    # the ``_invoke_sub_gates`` BacktestResult constructor uses when the
    # payload omits this key).
    equity_curve = (
        [round(float(v), 4) for v in result.equity_curve.tolist()]
        if result.equity_curve is not None
        else []
    )

    artifact = {
        "pnl_ntd": pnl_ntd,
        "pnl_pts": round(total_pnl_pts, 4),
        "fills": total_fills,
        "winning_days": sc.get("winning_days", 0),
        "n_days": sc.get("n_days", 0),
        "sharpe_is": round(float(result.sharpe_is), 4),
        "max_drawdown": round(float(result.max_drawdown), 6),
        "daily_pnl": result.daily_pnl or [],
        "equity_curve": equity_curve,
        "residual_mtm_pts": round(float(getattr(result, "residual_mtm_pts", 0.0)), 4),
        "residual_qty": int(getattr(result, "residual_qty", 0)),
        "mark_method": str(getattr(result, "mark_method", "last_mid")),
        "fixture_id": _FIXTURE_ID,
        "instrument": _INSTRUMENT,
        "latency_profile": _LATENCY_PROFILE,
        "latency_place_ms": float(profile["submit_ack_latency_ms"]),
        "latency_cancel_ms": float(profile["cancel_ack_latency_ms"]),
        "queue_fraction": 0.5,
        "queue_fraction_table": str(_QHAT_PATH),
        "strategy": "TmfD6SoloMakerMinimal(C60Params())",
        "captured_at": date.today().isoformat(),
        "captured_by": "slice-b-task-15",
    }
    out = Path("tests/fixtures/maker_engine_pre_mtm_baseline/r47_tmfd6_31d_post_b.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(artifact, sort_keys=True, indent=2) + "\n")
    cost_floor_total_ntd = _COST_FLOOR_PER_FILL_NTD * total_fills
    print(
        f"Wrote {out}: pnl_ntd={pnl_ntd}, fills={total_fills}, "
        f"sharpe={artifact['sharpe_is']}, residual_mtm_pts="
        f"{artifact['residual_mtm_pts']}, residual_qty={artifact['residual_qty']}"
    )
    print(
        f"DoD-B1 check: pnl_ntd={pnl_ntd:.2f} <= "
        f"cost_floor_total={cost_floor_total_ntd:.2f}? "
        f"{'PASS' if pnl_ntd <= cost_floor_total_ntd else 'FAIL'}"
    )

    if total_fills != _EXPECTED_FILLS:
        print(
            f"WARN: fill count {total_fills} != expected {_EXPECTED_FILLS} "
            "(should be invariant under MtM).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
