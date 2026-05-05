"""DoD-B5 lock-in: Slice C's replay_parity_gate must still produce the same
verdict on Slice C's existing fixtures even after Slice B's MakerEngine changes.

Three test cases:

1. R47 synthetic divergence fixture (94/100 line-count, computed match_pct=13.0)
   under MtM-aware MakerEngine -> ``replay_parity`` sub-gate FAILS.
2. Clean 1-tick echo fixture (100/100, match_pct=100.0) -> ``replay_parity``
   sub-gate PASSES.
3. Smoke: on the R47 fixture, the maker_realism gates (``inventory_mtm``,
   ``cost_uncertainty``) and ``replay_parity`` all co-fire FAIL on a single
   payload that pairs the divergent parity report with an R47-shaped
   ``daily_pnl`` that has both residual MtM and high day-to-day variance.

Slice B (2026-05-05): post-Task-13 ``MakerStrategyBridge.on_session_end`` emits
new ``IntentType.FORCE_FLAT`` intents. This test ensures those do NOT shift the
parity gate's verdict on Slice C's pre-existing fixtures (which contain only
intra-session intents — session-end residual close-out is not represented in
the JSONL fixtures, so the bookkeeping in :mod:`hft_platform.alpha.replay_parity`
stays canonical).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hft_platform.alpha._gate_c import _invoke_sub_gates
from hft_platform.alpha._validation_profile import load_profile
from hft_platform.alpha.replay_parity import IntentDiff
from hft_platform.replay.intent_log import ReplayedIntentLog

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "replay_parity"


@pytest.fixture(scope="module")
def strict_profile() -> Any:
    """Load vm_ul6_strict.yaml — same path Slice C's e2e tests use."""
    return load_profile("config/research/profiles/vm_ul6_strict.yaml")


def _r47_payload_with_daily_pnl(
    *,
    replay_parity_report: Any,
    daily_pnl: list[dict],
) -> dict:
    """R47-OE1 payload skeleton with caller-supplied parity report + daily_pnl.

    Mirrors :func:`tests.integration.test_strict_profile_e2e._r47_payload` so the
    sub-gate population matches Slice A's known-bad R47 fingerprint, but lets
    the caller inject a Slice-B-shaped ``daily_pnl`` (dict rows with ``fills``
    and ``residual_mtm_pts``) so :class:`InventoryMtMGate` sees usable data.
    """
    return {
        "run_id": "test_dod_b5",
        "config_hash": "abc",
        "instrument": "TMFD6",
        "strategy_name": "r47_maker_tmf",
        "engine": "maker_engine",
        "queue_model": "QueueDepletionFill(qf=0.5)",
        "calibration_profile_id": "uncalibrated",
        "data_source": "ck",
        "latency_profile": "shioaji_measured_p95",
        "pnl_pts": float(sum(row["pnl_pts"] for row in daily_pnl)),
        "n_fills": int(sum(row["fills"] for row in daily_pnl)),
        "n_trading_days": len(daily_pnl),
        "equity_curve": None,
        "pnl_per_fill": 61.5,
        "adverse_fill_pct": 0.30,
        "fill_rate_per_day": 1.26,
        "daily_pnl": daily_pnl,
        "replay_parity_report": replay_parity_report,
    }


def _build_r47_daily_pnl_dict_rows() -> list[dict]:
    """R47-shaped 31-day daily_pnl with single-day dominance + negative residual.

    Day 1 carries the realized profit and a large negative residual MtM (the
    un-FIFO'd inventory from the day-end position close-out modelled in Slice
    B Task 3). Days 2–31 are tiny losses with one fill each — produces the
    high-variance, single-day-dominance signature that fails both
    :class:`InventoryMtMGate` and :class:`CostUncertaintyGate` simultaneously.

    Numbers chosen so that:
      - ``sum(pnl_pts) + sum(residual_mtm_pts) < cost_floor_per_fill_pts * n_fills``
        (inventory_mtm FAIL: net=-30 < cost_floor_total=19.5)
      - daily P95 lower bound is negative (cost_uncertainty FAIL).
    """
    rows = [{"pnl_pts": 2400.0, "fills": 10, "residual_mtm_pts": -2400.0}]
    rows.extend({"pnl_pts": -1.0, "fills": 1, "residual_mtm_pts": 0.0} for _ in range(30))
    return rows


@pytest.mark.integration
def test_dod_b5_r47_synthetic_divergence_still_fails_after_slice_b(
    strict_profile: Any,
) -> None:
    """The Slice C R47 synthetic fixture must still produce a FAILING parity
    sub-gate on the strict profile after Slice B's MakerEngine changes.

    Headroom check: report match_pct vs the 95.0 threshold so future drift
    (e.g. from session-end intent emit shifting the canonical record set) is
    visible immediately in test output.
    """
    live = ReplayedIntentLog.from_jsonl(FIXTURE_DIR / "synthetic_r47_oe1_live.jsonl")
    replayed = ReplayedIntentLog.from_jsonl(
        FIXTURE_DIR / "synthetic_r47_oe1_replayed.jsonl"
    )
    report = IntentDiff(
        live=live.canonical_records(),
        replayed=replayed.canonical_records(),
    ).compute()

    # Sanity: synthetic R47-OE1 fixture is designed to diverge well below
    # the 95% threshold. Keep this loose so any small canonicalisation
    # change in Slice C still leaves the gate firing on real diff data.
    assert report.match_pct < 95.0, report
    headroom = 95.0 - report.match_pct
    print(
        f"[DoD-B5] R47 fixture match_pct={report.match_pct:.4f} "
        f"(threshold=95.0, headroom={headroom:.4f}pp)"
    )

    payload = _r47_payload_with_daily_pnl(
        replay_parity_report=report,
        daily_pnl=_build_r47_daily_pnl_dict_rows(),
    )
    thresholds = strict_profile.thresholds_for(strategy_type="maker")
    advisory, blocking = _invoke_sub_gates(
        strategy_type="maker",
        result_payload=payload,
        thresholds=thresholds,
        profile=strict_profile,
    )

    assert blocking is not None
    assert blocking["passed"] is False, blocking
    failing_names = {f["name"] for f in blocking["failing"]}
    assert "replay_parity" in failing_names, blocking["failing"]


@pytest.mark.integration
def test_dod_b5_clean_echo_still_passes_after_slice_b(
    strict_profile: Any,
) -> None:
    """Clean echo fixture (100/100) must still produce a 100% match_pct and
    therefore a PASSING ``replay_parity`` sub-gate. Other strict gates may
    still fail (the R47 fingerprint payload skeleton is intentionally bad);
    only the parity gate's pass status is under test here.
    """
    live = ReplayedIntentLog.from_jsonl(FIXTURE_DIR / "clean_echo_live.jsonl")
    replayed = ReplayedIntentLog.from_jsonl(FIXTURE_DIR / "clean_echo_replayed.jsonl")
    report = IntentDiff(
        live=live.canonical_records(),
        replayed=replayed.canonical_records(),
    ).compute()

    assert report.match_pct == 100.0, report

    payload = _r47_payload_with_daily_pnl(
        replay_parity_report=report,
        daily_pnl=_build_r47_daily_pnl_dict_rows(),
    )
    thresholds = strict_profile.thresholds_for(strategy_type="maker")
    advisory, blocking = _invoke_sub_gates(
        strategy_type="maker",
        result_payload=payload,
        thresholds=thresholds,
        profile=strict_profile,
    )

    assert blocking is not None
    failing_names = {f["name"] for f in blocking["failing"]}
    assert "replay_parity" not in failing_names, (
        f"replay_parity should NOT block on 100% match; "
        f"failing={blocking['failing']}"
    )
    advisory_names = {g["name"] for g in advisory}
    assert "replay_parity" in advisory_names, advisory_names


@pytest.mark.integration
def test_dod_b5_maker_realism_gates_co_fire_on_r47(strict_profile: Any) -> None:
    """Smoke: on the R47 divergence fixture, ``inventory_mtm`` +
    ``cost_uncertainty`` + ``replay_parity`` all fire FAIL on a single payload.

    The R47 ``daily_pnl`` is constructed in Slice-B dict-row shape so the
    new gates have the data they need (Slice C's e2e fixture used a flat
    float-list which would have triggered the legacy-shape advisory PASS in
    :class:`InventoryMtMGate`).
    """
    live = ReplayedIntentLog.from_jsonl(FIXTURE_DIR / "synthetic_r47_oe1_live.jsonl")
    replayed = ReplayedIntentLog.from_jsonl(
        FIXTURE_DIR / "synthetic_r47_oe1_replayed.jsonl"
    )
    report = IntentDiff(
        live=live.canonical_records(),
        replayed=replayed.canonical_records(),
    ).compute()

    payload = _r47_payload_with_daily_pnl(
        replay_parity_report=report,
        daily_pnl=_build_r47_daily_pnl_dict_rows(),
    )
    thresholds = strict_profile.thresholds_for(strategy_type="maker")
    advisory, blocking = _invoke_sub_gates(
        strategy_type="maker",
        result_payload=payload,
        thresholds=thresholds,
        profile=strict_profile,
    )

    assert blocking is not None
    assert blocking["passed"] is False, blocking
    failing_names = {f["name"] for f in blocking["failing"]}
    assert "replay_parity" in failing_names, failing_names
    assert "inventory_mtm" in failing_names, failing_names
    assert "cost_uncertainty" in failing_names, failing_names
