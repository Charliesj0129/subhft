"""DoD-B1 + DoD-B2 end-to-end evidence (Slice B Task 15).

Two integration tests on the post-Slice-B promotion pipeline:

1. ``test_dod_b1_post_b_pnl_below_cost_floor`` — given the post-B baseline
   artifact (captured by ``scripts/capture_post_b_baseline.py`` against the
   live ClickHouse-backed simulator), assert that R47/TMFD6/31d's PnL under
   the MtM-aware ``MakerEngine`` and calibrated ``QHatTable`` collapses to or
   below the maker cost floor. This was the central credibility claim of the
   2026-04-24 audit (``docs/incidents/2026-04-24-r47-backtest-credibility-audit.md``)
   that Slice B is meant to operationalise.

2. ``test_dod_b2_inventory_mtm_gate_fires_on_r47_passes_on_robust`` — invoke
   ``_invoke_sub_gates`` against the same R47 post-B payload and a synthetic
   "robust alpha" payload using the strict profile, and assert the new
   ``inventory_mtm`` and ``cost_uncertainty`` sub-gates correctly fire FAIL
   on R47 (single-day-dominance + residual MtM signature) and PASS on the
   robust fixture (steady realised + zero residual).

Cost floor source
-----------------
``vm_ul6_strict.yaml :: thresholds.maker.cost_floor_per_fill_pts = 0.5``
(Slice B Task 11). For TMFD6 the point value is 10 NTD/contract (memory:
``feedback_mini_taiex_point_value.md``), so the per-fill NTD cost floor is
``0.5 × 10 = 5.0`` NTD. With 39 pre-B fills that yields ``195`` NTD; the
post-B fill count may differ from 39 because Slice B Task 8's q_hat lookup
replaces the literal ``queue_fraction=0.5`` and shifts depleted-queue
mechanics, so the cost-floor *total* in the test is computed from the
**post-B** fill count, not the pre-B one.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

import pytest

from hft_platform.alpha._gate_c import _invoke_sub_gates
from hft_platform.alpha._validation_profile import load_profile

FIXTURE_ROOT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "maker_engine_pre_mtm_baseline"
)

# ``vm_ul6_strict.yaml :: thresholds.maker.cost_floor_per_fill_pts`` × TMFD6
# point-value (10 NTD/contract). Keep both numbers visible for auditability.
COST_FLOOR_PER_FILL_PTS: float = 0.5
TMFD6_POINT_VALUE_NTD: int = 10
COST_FLOOR_PER_FILL_NTD: float = COST_FLOOR_PER_FILL_PTS * TMFD6_POINT_VALUE_NTD


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURE_ROOT / name).read_text())


def _build_payload(artifact: dict[str, Any], *, run_id: str) -> dict[str, Any]:
    """Construct the ``_invoke_sub_gates`` ``result_payload`` dict.

    Mirrors the shape used by ``tests/integration/test_replay_parity_post_slice_b.py``
    (Task 14): pass through the artifact's ``daily_pnl`` (Slice-B dict rows)
    and the real ``equity_curve`` (so Slice A blocking gates such as
    ``day_bootstrap_ci`` see a non-trivial array rather than the
    ``np.zeros(1)`` placeholder).
    """
    return {
        "run_id": run_id,
        "config_hash": artifact.get("fixture_id", ""),
        "instrument": artifact.get("instrument", "TMFD6"),
        "strategy_name": artifact.get("strategy", ""),
        "engine": "maker_engine",
        "queue_model": f"QueueDepletionFill(qf={artifact.get('queue_fraction', 0.5)})",
        "calibration_profile_id": str(
            artifact.get("queue_fraction_table", "uncalibrated")
        ),
        "data_source": "clickhouse_direct",
        "latency_profile": str(artifact.get("latency_profile", "")),
        "pnl_pts": float(artifact.get("pnl_pts", 0.0)),
        "n_fills": int(artifact.get("fills", 0)),
        "n_trading_days": int(artifact.get("n_days", 0)),
        "equity_curve": list(artifact.get("equity_curve") or []),
        "daily_pnl": list(artifact.get("daily_pnl") or []),
    }


@pytest.fixture(scope="module")
def strict_profile() -> Any:
    return load_profile("config/research/profiles/vm_ul6_strict.yaml")


@pytest.fixture(scope="module")
def post_b_artifact() -> dict[str, Any]:
    return _load_fixture("r47_tmfd6_31d_post_b.json")


@pytest.fixture(scope="module")
def pre_b_artifact() -> dict[str, Any]:
    return _load_fixture("r47_tmfd6_31d_pre_b.json")


@pytest.fixture(scope="module")
def robust_artifact() -> dict[str, Any]:
    return _load_fixture("robust_alpha_synthetic.json")


@pytest.mark.integration
def test_dod_b1_post_b_pnl_below_cost_floor(
    pre_b_artifact: dict[str, Any],
    post_b_artifact: dict[str, Any],
) -> None:
    """DoD-B1: under the MtM-aware MakerEngine + calibrated QHatTable, R47/TMFD6/31d
    PnL must collapse to at most the maker cost floor.

    The cost floor total scales with **post-B** ``fills`` (not pre-B 39), since
    Slice B Task 8's q_hat-driven queue mechanics shift the depleted-queue
    bookkeeping and may change fill count. Note this also relaxes the original
    "MtM is fill-count-invariant" claim: MtM alone is invariant, but the
    *paired* Slice B change (q_hat-replacing-literal-0.5) is not. The test
    therefore documents the magnitude shift rather than asserting strict
    fill-count equality.
    """
    pnl_ntd = float(post_b_artifact["pnl_ntd"])
    n_fills = int(post_b_artifact["fills"])
    cost_floor_total_ntd = COST_FLOOR_PER_FILL_NTD * n_fills

    assert pnl_ntd <= cost_floor_total_ntd, (
        f"DoD-B1 violation: post-B PnL {pnl_ntd:+.1f} NTD exceeds cost floor "
        f"{COST_FLOOR_PER_FILL_NTD} × {n_fills} fills = "
        f"{cost_floor_total_ntd:.1f} NTD"
    )

    pre_b_pnl = float(pre_b_artifact["pnl_ntd"])
    pre_b_fills = int(pre_b_artifact["fills"])
    delta_ntd = pnl_ntd - pre_b_pnl
    print(
        f"\n[DoD-B1] R47 TMFD6 31d magnitude shift: "
        f"pre_b={pre_b_pnl:+.0f} NTD ({pre_b_fills} fills) -> "
        f"post_b={pnl_ntd:+.0f} NTD ({n_fills} fills); "
        f"delta={delta_ntd:+.0f} NTD; "
        f"cost_floor_total={cost_floor_total_ntd:.0f} NTD; "
        f"residual_mtm_pts={post_b_artifact.get('residual_mtm_pts'):+.1f}"
    )


@pytest.mark.integration
def test_dod_b2_inventory_mtm_gate_fires_on_r47_passes_on_robust(
    strict_profile: Any,
    post_b_artifact: dict[str, Any],
    robust_artifact: dict[str, Any],
) -> None:
    """DoD-B2: ``inventory_mtm`` and ``cost_uncertainty`` must FAIL on the
    R47 post-B payload (single-day dominance + negative residual MtM) and
    PASS on a synthetic robust-alpha payload (steady realised, zero
    residual).

    Only the two new Slice B sub-gates are asserted here; other strict-profile
    blocking gates (e.g. min_sample_size on the robust 30-day fixture) may or
    may not fire and are not under test in this DoD.
    """
    thresholds = strict_profile.thresholds_for(strategy_type="maker")

    r47_advisory, _ = _invoke_sub_gates(
        strategy_type="maker",
        result_payload=_build_payload(post_b_artifact, run_id="task15-r47-post-b"),
        thresholds=thresholds,
        profile=strict_profile,
    )
    robust_advisory, _ = _invoke_sub_gates(
        strategy_type="maker",
        result_payload=_build_payload(robust_artifact, run_id="task15-robust"),
        thresholds=thresholds,
        profile=strict_profile,
    )

    r47_by_name = {entry["name"]: entry for entry in r47_advisory}
    robust_by_name = {entry["name"]: entry for entry in robust_advisory}

    # R47 post-B: both new gates must FAIL.
    assert r47_by_name["inventory_mtm"]["passed"] is False, (
        f"R47 inventory_mtm should FAIL: {r47_by_name['inventory_mtm']}"
    )
    assert r47_by_name["cost_uncertainty"]["passed"] is False, (
        f"R47 cost_uncertainty should FAIL: {r47_by_name['cost_uncertainty']}"
    )

    # Robust synthetic: both new gates must PASS.
    assert robust_by_name["inventory_mtm"]["passed"] is True, (
        f"Robust inventory_mtm should PASS: {robust_by_name['inventory_mtm']}"
    )
    assert robust_by_name["cost_uncertainty"]["passed"] is True, (
        f"Robust cost_uncertainty should PASS: {robust_by_name['cost_uncertainty']}"
    )

    # Sanity: the synthetic robust fixture really does sit above its CI floor.
    pnl_series = [
        float(row["pnl_pts"])
        for row in robust_artifact["daily_pnl"]
        if int(row.get("fills", 0)) > 0
    ]
    if len(pnl_series) >= 2:
        mu = statistics.mean(pnl_series)
        sigma = statistics.stdev(pnl_series)
        sem = sigma / (len(pnl_series) ** 0.5)
        p95_lower = mu - 1.645 * sem
        assert p95_lower > 0.0, (
            f"robust fixture mis-constructed: P95 lower bound {p95_lower:.4f} "
            f"is non-positive (mu={mu}, sigma={sigma})"
        )
