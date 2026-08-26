"""A strategy quarantine is a safety latch; a restart must not release it.

Before ``restore_persisted_quarantines`` existed, ``StrategyHealthGovernor``
booted with an empty dict. The persisted document still recorded the latch, but
nothing read it back, so a restart was an unauthenticated re-arm *and* the
``ManualRearmRequired`` alert resolved itself. Observed in production
2026-08-25 on ``R47_MAKER_TMF``.

The restore is *hydration*, not a new quarantine: it reuses the persisted
token, so an operator re-arm request published before the restart still clears
the latch it was issued for. The first version of this fix minted a fresh token
instead, which silently destroyed that request.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hft_platform.ops.manual_rearm import ManualRearmGate
from hft_platform.ops.runtime_state_store import RuntimeStateUnreadable
from hft_platform.ops.strategy_governor import (
    StrategyHealthGovernor,
    StrategyQuarantineStateCorrupt,
)

_SID = "R47_MAKER_TMF"


def _write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _latched(
    strategy_id: str = _SID,
    reason: str = "strategy_exception",
    token: str = "prev-run:R47_MAKER_TMF:1",
) -> dict:
    return {
        "platform": {"manual_rearm_required": False, "reason": None},
        "strategies": {
            strategy_id: {
                "manual_rearm_required": True,
                "reason": reason,
                "quarantine_token": token,
            }
        },
    }


def test_a_quarantined_strategy_is_still_quarantined_after_a_restart(tmp_path: Path) -> None:
    state = tmp_path / "runtime_state.json"
    _write_state(state, _latched())

    governor = StrategyHealthGovernor()
    restored = governor.restore_persisted_quarantines(state_path=state)

    assert restored == [_SID]
    assert governor.is_quarantined(_SID) is True


def test_a_restored_quarantine_keeps_the_token_the_previous_run_issued(tmp_path: Path) -> None:
    """The identity of the latch is persisted state, not a per-run value.

    Minting a new token here is what invalidated in-flight operator requests.
    """
    state = tmp_path / "runtime_state.json"
    _write_state(state, _latched(token="run-a:R47_MAKER_TMF:7"))

    governor = StrategyHealthGovernor()
    governor.restore_persisted_quarantines(state_path=state)

    assert governor.quarantine_token(_SID) == "run-a:R47_MAKER_TMF:7"


def test_a_re_arm_request_written_before_the_restart_still_clears_the_latch(tmp_path: Path) -> None:
    """The operator authorized *this* latch; a restart does not revoke that.

    This is the behaviour the first version of the fix had exactly backwards.
    """
    state = tmp_path / "runtime_state.json"
    _write_state(state, _latched(token="run-a:R47_MAKER_TMF:1"))

    governor = StrategyHealthGovernor()
    governor.restore_persisted_quarantines(state_path=state)

    # The token the operator's pre-restart request would have named.
    assert governor.rearm(_SID, expected_token="run-a:R47_MAKER_TMF:1") is True
    assert governor.is_quarantined(_SID) is False


def test_a_request_naming_an_earlier_quarantine_cannot_clear_a_later_one(tmp_path: Path) -> None:
    """The token still does its real job: instance N's authorization is not instance N+1's."""
    state = tmp_path / "runtime_state.json"
    _write_state(state, _latched(token="run-a:R47_MAKER_TMF:1"))

    governor = StrategyHealthGovernor()
    governor.restore_persisted_quarantines(state_path=state)
    assert governor.rearm(_SID, expected_token="run-a:R47_MAKER_TMF:1") is True

    # A genuinely new failure, in this run.
    governor.quarantine(_SID, reason="strategy_exception")
    assert governor.rearm(_SID, expected_token="run-a:R47_MAKER_TMF:1") is False
    assert governor.is_quarantined(_SID) is True


def test_a_quarantine_minted_after_a_rebuild_does_not_reuse_the_previous_instances_token() -> None:
    """Two governors in one process must not issue the same token.

    A module-scoped run id plus a per-instance counter starting at 1 gave the
    first quarantine of a rebuilt governor a token the previous instance had
    already issued.
    """
    first = StrategyHealthGovernor()
    first.quarantine(_SID, reason="strategy_exception")

    second = StrategyHealthGovernor()
    second.quarantine(_SID, reason="strategy_exception")

    assert first.quarantine_token(_SID) != second.quarantine_token(_SID)


def test_restoring_writes_nothing_to_the_state_document(tmp_path: Path) -> None:
    """Hydration is idempotent: a restart loop must not grow the evidence file."""
    state = tmp_path / "runtime_state.json"
    _write_state(state, _latched())
    before = state.read_bytes()

    for _ in range(3):
        StrategyHealthGovernor().restore_persisted_quarantines(state_path=state)

    assert state.read_bytes() == before


def test_a_strategy_not_latched_in_the_persisted_state_is_not_quarantined(tmp_path: Path) -> None:
    state = tmp_path / "runtime_state.json"
    _write_state(
        state,
        {
            "platform": {"manual_rearm_required": False, "reason": None},
            "strategies": {_SID: {"manual_rearm_required": False, "reason": None}},
        },
    )

    governor = StrategyHealthGovernor()
    assert governor.restore_persisted_quarantines(state_path=state) == []
    assert governor.is_quarantined(_SID) is False


def test_a_missing_state_document_is_a_cold_start_and_restores_nothing(tmp_path: Path) -> None:
    governor = StrategyHealthGovernor()
    assert governor.restore_persisted_quarantines(state_path=tmp_path / "absent.json") == []


def test_an_unreadable_state_document_refuses_rather_than_reading_as_all_clear(tmp_path: Path) -> None:
    state = tmp_path / "runtime_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{not json", encoding="utf-8")

    governor = StrategyHealthGovernor()
    with pytest.raises(RuntimeStateUnreadable):
        governor.restore_persisted_quarantines(state_path=state)


@pytest.mark.parametrize("section", [[], "latched", 3])
def test_a_valid_document_with_a_malformed_strategies_section_refuses(tmp_path: Path, section: object) -> None:
    """Syntactically valid JSON of the wrong shape is corruption, not 'nothing latched'.

    Caught at the store layer, which owns document shape; the governor owns the
    shape of the individual latch records below.
    """
    state = tmp_path / "runtime_state.json"
    _write_state(state, {"platform": {"manual_rearm_required": False}, "strategies": section})

    governor = StrategyHealthGovernor()
    with pytest.raises(RuntimeStateUnreadable):
        governor.restore_persisted_quarantines(state_path=state)


def test_a_malformed_strategies_section_supplied_directly_still_refuses() -> None:
    """Defence in depth: the governor does not trust a snapshot it did not read."""
    governor = StrategyHealthGovernor()
    with pytest.raises(StrategyQuarantineStateCorrupt):
        governor.restore_persisted_quarantines(snapshot={"platform": {}, "strategies": []})


def test_a_malformed_strategy_entry_refuses_rather_than_being_skipped(tmp_path: Path) -> None:
    state = tmp_path / "runtime_state.json"
    _write_state(state, {"platform": {}, "strategies": {_SID: "quarantined"}})

    governor = StrategyHealthGovernor()
    with pytest.raises(StrategyQuarantineStateCorrupt):
        governor.restore_persisted_quarantines(state_path=state)


def test_every_latched_strategy_is_restored_not_only_the_first(tmp_path: Path) -> None:
    state = tmp_path / "runtime_state.json"
    _write_state(
        state,
        {
            "platform": {"manual_rearm_required": False, "reason": None},
            "strategies": {
                "A": {"manual_rearm_required": True, "reason": "a", "quarantine_token": "r:A:1"},
                "B": {"manual_rearm_required": False, "reason": None},
                "C": {"manual_rearm_required": True, "reason": "c", "quarantine_token": "r:C:1"},
            },
        },
    )

    governor = StrategyHealthGovernor()
    assert governor.restore_persisted_quarantines(state_path=state) == ["A", "C"]


def test_a_supplied_snapshot_is_used_without_reading_the_filesystem(tmp_path: Path) -> None:
    """bootstrap reads the document before any startup side effect exists to unwind."""
    governor = StrategyHealthGovernor()
    restored = governor.restore_persisted_quarantines(state_path=tmp_path / "never-created.json", snapshot=_latched())
    assert restored == [_SID]


def test_the_gate_and_the_governor_agree_on_the_token_for_the_same_document(tmp_path: Path) -> None:
    """Read side and write side of the authorization protocol must not split."""
    state = tmp_path / "runtime_state.json"
    _write_state(state, _latched(token="run-a:R47_MAKER_TMF:2"))

    governor = StrategyHealthGovernor()
    governor.restore_persisted_quarantines(state_path=state)

    assert ManualRearmGate(state_path=state).snapshot()["strategies"][_SID]["quarantine_token"] == (
        governor.quarantine_token(_SID)
    )


def test_a_latch_written_before_tokens_existed_is_restored_not_refused(tmp_path: Path) -> None:
    """The upgrade that introduces tokens must not turn a live latch into a crash loop.

    The production document on 2026-08-26 carried exactly this shape for
    ``R47_MAKER_TMF``: latched, with a reason, and no ``quarantine_token``.
    """
    state = tmp_path / "runtime_state.json"
    _write_state(
        state,
        {
            "platform": {"manual_rearm_required": False, "reason": None},
            "strategies": {_SID: {"manual_rearm_required": True, "reason": "strategy_exception"}},
        },
    )

    governor = StrategyHealthGovernor()
    assert governor.restore_persisted_quarantines(state_path=state) == [_SID]
    assert governor.is_quarantined(_SID) is True


def test_a_legacy_latch_is_given_a_token_it_can_be_re_armed_with(tmp_path: Path) -> None:
    """A latch with no identity could never be cleared through the authorized path."""
    state = tmp_path / "runtime_state.json"
    _write_state(
        state,
        {
            "platform": {"manual_rearm_required": False, "reason": None},
            "strategies": {_SID: {"manual_rearm_required": True, "reason": "strategy_exception"}},
        },
    )

    governor = StrategyHealthGovernor()
    governor.restore_persisted_quarantines(state_path=state)

    persisted = json.loads(state.read_text(encoding="utf-8"))["strategies"][_SID]["quarantine_token"]
    assert persisted == governor.quarantine_token(_SID)
    assert governor.rearm(_SID, expected_token=persisted) is True


def test_the_legacy_migration_runs_once_and_is_stable_across_boots(tmp_path: Path) -> None:
    state = tmp_path / "runtime_state.json"
    _write_state(
        state,
        {
            "platform": {"manual_rearm_required": False, "reason": None},
            "strategies": {_SID: {"manual_rearm_required": True, "reason": "strategy_exception"}},
        },
    )

    StrategyHealthGovernor().restore_persisted_quarantines(state_path=state)
    after_first = state.read_bytes()
    for _ in range(3):
        StrategyHealthGovernor().restore_persisted_quarantines(state_path=state)

    assert state.read_bytes() == after_first
