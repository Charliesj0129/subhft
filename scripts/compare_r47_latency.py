"""P0 comparison driver — R47 MakerEngine PnL under three latency regimes.

Answers: "When the v2026-04-24 measured live-broker latency profile is injected
into MakerEngine, does the R47-minimal backtest PnL stay positive?"

Runs TmfD6SoloMakerMinimal (C60) on TMFD6 for all locally-available days via
MakerEngine.run() with:

  1. no latency      (instant-RTT baseline; equivalent to historical +7,701 regime)
  2. v2026-04-24     (210 ms place / 210 ms cancel — measured broker RTT P95)
  3. v2026-04-09     (36 ms place / 47 ms cancel — deprecated sim-mode profile)
  4. shioaji_p95()   (800 ms / 800 ms — D5 canned conservative upper bound)

Outputs a one-row-per-regime summary.

Invocation:
    CLICKHOUSE_PASSWORD=$(grep CLICKHOUSE_PASSWORD .env | cut -d= -f2) \\
        uv run python scripts/compare_r47_latency.py

Exit code 0 always (this is a reporting tool; caller interprets numbers).
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


def _resolve_bridge(name: str) -> LatencyProfile:
    p = resolve_profile(name)
    return LatencyProfile(
        place_ns=int(float(p["submit_ack_latency_ms"]) * 1_000_000),
        cancel_ns=int(float(p["cancel_ack_latency_ms"]) * 1_000_000),
    )


def _run_regime(label: str, latency_profile: LatencyProfile | None) -> dict:
    ck = ClickHouseSource()
    cost = load_cost_profile(_INSTRUMENT)
    fill = QueueDepletionFill(queue_fraction=0.5)
    engine = MakerEngine(
        fill_model=fill,
        cost_model=cost,
        ck_source=ck,
        latency_profile=latency_profile,
    )
    strategy = TmfD6SoloMakerMinimal(params=C60Params(), active_symbol=_INSTRUMENT)
    result = engine.run(strategy=strategy, instrument=_INSTRUMENT, pipeline_mode="strict")
    sc = result.maker_scorecard or {}
    total_pnl_pts = float(sc.get("total_pnl_pts", 0.0))
    total_fills = int(sc.get("total_fills", 0))
    return {
        "label": label,
        "place_ns": latency_profile.place_ns if latency_profile else 0,
        "cancel_ns": latency_profile.cancel_ns if latency_profile else 0,
        "total_pnl_pts": round(total_pnl_pts, 2),
        "total_pnl_ntd": round(total_pnl_pts * _POINT_VALUE_NTD, 2),
        "total_fills": total_fills,
        "pnl_per_fill_pts": round(
            total_pnl_pts / total_fills if total_fills else 0.0, 4
        ),
        "winning_days": sc.get("winning_days", 0),
        "n_days": sc.get("n_days", 0),
        "sharpe": round(result.sharpe_is, 3),
    }


def main() -> int:
    if not os.environ.get("CLICKHOUSE_PASSWORD"):
        print(
            "ERROR: CLICKHOUSE_PASSWORD is not set. "
            "Run: CLICKHOUSE_PASSWORD=$(grep CLICKHOUSE_PASSWORD .env | cut -d= -f2) "
            "uv run python scripts/compare_r47_latency.py",
            file=sys.stderr,
        )
        return 2

    regimes: list[tuple[str, LatencyProfile | None]] = [
        ("no_latency (instant-RTT baseline)", None),
        ("v2026-04-09 (sim 36/47ms — deprecated)", _resolve_bridge("r47_maker_shioaji_p95_v2026-04-09")),
        ("v2026-04-24 (derived 210/210ms)", _resolve_bridge("r47_maker_shioaji_p95_v2026-04-24")),
        ("v2026-04-24_measured (direct probe 395/59ms)", _resolve_bridge("r47_maker_shioaji_p95_v2026-04-24_measured")),
        ("shioaji_p95 canned (800/800ms)", LatencyProfile.shioaji_p95()),
    ]

    rows = [_run_regime(label, profile) for label, profile in regimes]

    print("\n=== R47 (C60 TmfD6SoloMakerMinimal) TMFD6 latency sensitivity ===")
    print(
        f"{'regime':<40}{'place_ms':>10}{'cancel_ms':>10}"
        f"{'fills':>8}{'pnl_pts':>12}{'pnl_ntd':>12}{'sharpe':>8}"
    )
    for r in rows:
        print(
            f"{r['label']:<40}"
            f"{r['place_ns'] / 1_000_000:>10.1f}"
            f"{r['cancel_ns'] / 1_000_000:>10.1f}"
            f"{r['total_fills']:>8}"
            f"{r['total_pnl_pts']:>12.2f}"
            f"{r['total_pnl_ntd']:>12.2f}"
            f"{r['sharpe']:>8.3f}"
        )
    print()
    print("JSON:")
    print(json.dumps(rows, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
