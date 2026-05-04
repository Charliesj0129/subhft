"""End-to-end Slice C replay-parity integration tests.

DoD-C1: synthetic R47-OE1 fixtures (live + replayed) drive `IntentDiff` and
the resulting low-match-pct report blocks promotion via the strict profile's
`replay_parity` sub-gate (exercised through `_invoke_sub_gates`, NOT the gate
in isolation — the test must prove the gate is wired into the profile's
`blocking_sub_gates` aggregate).

DoD-C2: clean-echo fixtures produce a 100% match report and the
`replay_parity` gate is NOT among the blocking-failures (other strict gates
may still fail on the synthetic payload — only the parity gate's pass is
under test here).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hft_platform.alpha._gate_c import _invoke_sub_gates
from hft_platform.alpha._validation_profile import load_profile
from hft_platform.alpha.replay_parity import IntentDiff
from hft_platform.replay.intent_log import ReplayedIntentLog
from tests.integration.test_strict_profile_e2e import (
    _ParityReport,
    _r47_payload,
    _robust_payload,
)

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "replay_parity"


@pytest.fixture(scope="module")
def strict_profile() -> Any:
    return load_profile("config/research/profiles/vm_ul6_strict.yaml")


def _r47_payload_with(*, replay_parity_report: Any) -> dict:
    """R47-OE1 payload with caller-supplied parity report.

    Reuses the canonical `_r47_payload()` skeleton so the rest of the strict
    sub-gates see the same fingerprint as the Slice A test suite — only
    `replay_parity_report` differs.
    """
    payload = _r47_payload()
    payload["replay_parity_report"] = replay_parity_report
    return payload


def _clean_payload_with(*, replay_parity_report: Any) -> dict:
    """Robust payload with caller-supplied parity report.

    Built from `_robust_payload()` so the strict statistical sub-gates
    (min_sample_size, single_day_dominance, loo_day_sensitivity) pass and
    the only assertion under test is whether `replay_parity` blocks.
    """
    payload = _robust_payload()
    payload["replay_parity_report"] = replay_parity_report
    return payload


@pytest.mark.integration
def test_dod_c1_synthetic_r47_kills_at_replay_parity_gate(
    strict_profile: Any,
) -> None:
    """DoD-C1: synthetic R47-OE1 divergence -> strict profile KILLS via
    replay_parity sub-gate (proven through profile aggregation).
    """
    live = ReplayedIntentLog.from_jsonl(FIXTURE_DIR / "synthetic_r47_oe1_live.jsonl")
    replayed = ReplayedIntentLog.from_jsonl(FIXTURE_DIR / "synthetic_r47_oe1_replayed.jsonl")
    report = IntentDiff(
        live=live.canonical_records(),
        replayed=replayed.canonical_records(),
    ).compute()

    # Sanity: synthetic R47-OE1 fixtures are designed to diverge well below
    # the 95% threshold, so the gate should fire on real diff data.
    assert report.match_pct < 95.0, report

    payload = _r47_payload_with(replay_parity_report=report)
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
def test_dod_c2_clean_echo_passes(strict_profile: Any) -> None:
    """DoD-C2: clean-echo fixtures -> 100% match -> replay_parity does NOT
    block. Other strict gates may still fail on the synthetic payload; the
    only assertion is that `replay_parity` itself is not in the failing set.
    """
    live = ReplayedIntentLog.from_jsonl(FIXTURE_DIR / "clean_echo_live.jsonl")
    replayed = ReplayedIntentLog.from_jsonl(FIXTURE_DIR / "clean_echo_replayed.jsonl")
    report = IntentDiff(
        live=live.canonical_records(),
        replayed=replayed.canonical_records(),
    ).compute()

    assert report.match_pct == 100.0, report

    payload = _clean_payload_with(replay_parity_report=report)
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
        f"replay_parity should NOT block on clean echo; failing={blocking['failing']}"
    )
    # And the gate appeared in the advisory list (i.e. it was actually run).
    advisory_names = {g["name"] for g in advisory}
    assert "replay_parity" in advisory_names, advisory_names
    # Verify _ParityReport import path is exercised (silences unused-import
    # warning while documenting the helper relationship across both files).
    assert _ParityReport.__name__ == "_ParityReport"


@pytest.mark.integration
def test_loose_profile_does_not_block_on_replay_parity() -> None:
    """DoD-C3: under a loose (no-profile) call, `replay_parity` MUST stay
    advisory only — `blocking is None` and the gate appears in the advisory
    list (proving registration via `ensure_builtin_sub_gates_registered()`,
    which `_invoke_sub_gates` calls internally).
    """
    # No replay_parity_report attached: the gate runs in advisory mode and
    # reports failure on missing report, but MUST NOT block when profile is
    # None — that's the loose-profile non-regression invariant.
    payload = _r47_payload_with(replay_parity_report=None)
    advisory, blocking = _invoke_sub_gates(
        strategy_type="maker",
        result_payload=payload,
        thresholds={
            "sharpe_is_min": 0.5,
            "winning_day_pct_min": 55,
            "replay_parity_match_pct_min": 95.0,
        },
        profile=None,
    )

    assert blocking is None
    advisory_names = {g["name"] for g in advisory}
    assert "replay_parity" in advisory_names, advisory_names
