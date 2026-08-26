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
    parse_persisted_quarantines,
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
    _write_state(
        state,
        {"platform": {"manual_rearm_required": False, "reason": None}, "strategies": {_SID: "quarantined"}},
    )

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


# --- Review round 2: the three fail-open gaps Codex found -------------------
#
# All three share one shape: a reading the code could not honestly make was
# resolved in the trading direction. A latch that cannot be read is not an
# absent latch.


@pytest.mark.parametrize("section", ["platform", "strategies"])
def test_a_present_null_latch_section_refuses_rather_than_reading_as_clear(tmp_path: Path, section: str) -> None:
    """``raw.get(s)`` returns ``None`` for both "absent" and "present, null".

    Only the first is a cold start. Collapsing them let a document such as
    ``{"platform": null}`` normalize to ``manual_rearm_required=false`` and
    release a platform HALT latch on boot.
    """
    state = tmp_path / "runtime_state.json"
    payload: dict = {"platform": {"manual_rearm_required": True, "reason": "storm"}, "strategies": {}}
    payload[section] = None
    _write_state(state, payload)

    with pytest.raises(RuntimeStateUnreadable):
        ManualRearmGate(state_path=state).snapshot()


def test_an_existing_document_missing_the_strategies_section_refuses(tmp_path: Path) -> None:
    """This test used to assert the opposite, and the opposite was the bug.

    "Guard the fix from over-reaching" was the wrong instinct: a document that
    exists but has lost its ``strategies`` section cannot prove that no
    strategy was latched. Only a missing *file* proves a cold start.
    """
    state = tmp_path / "runtime_state.json"
    _write_state(state, {"platform": {"manual_rearm_required": False, "reason": None}})

    governor = StrategyHealthGovernor()
    with pytest.raises(RuntimeStateUnreadable):
        governor.restore_persisted_quarantines(state_path=state)


def test_no_document_at_all_is_a_cold_start(tmp_path: Path) -> None:
    """The one case that genuinely proves nothing was latched."""
    governor = StrategyHealthGovernor()

    assert governor.restore_persisted_quarantines(state_path=tmp_path / "absent.json") == []


@pytest.mark.parametrize("flag", [None, "", 0, 1, "true", "false", []])
def test_a_latched_entry_with_a_non_boolean_flag_refuses(tmp_path: Path, flag: object) -> None:
    """``bool(entry.get(...))`` collapsed damage into a deliberate ``False``.

    A record carrying a real reason and a real token, whose flag had been
    truncated to null, read as "this strategy is free to trade". Every writer
    of the field writes an actual bool, so anything else is damage.
    """
    state = tmp_path / "runtime_state.json"
    payload = _latched()
    payload["strategies"][_SID]["manual_rearm_required"] = flag
    _write_state(state, payload)

    governor = StrategyHealthGovernor()
    with pytest.raises(StrategyQuarantineStateCorrupt):
        governor.restore_persisted_quarantines(state_path=state)


def test_a_malformed_entry_is_rejected_by_the_pre_lease_validation(tmp_path: Path) -> None:
    """The startup gate must raise on *entries*, not only on the container.

    Until it did, a damaged record raised at the apply call instead -- after the
    Redis lease and its refresh thread were live -- so under ``restart: always``
    every attempt leaked a lease it held until TTL.
    """
    payload = _latched()
    payload["strategies"][_SID]["manual_rearm_required"] = None

    with pytest.raises(StrategyQuarantineStateCorrupt):
        parse_persisted_quarantines(payload)


def test_the_pre_lease_validation_accepts_a_well_formed_document(tmp_path: Path) -> None:
    parsed = parse_persisted_quarantines(_latched())

    assert list(parsed) == [_SID]
    assert parsed[_SID].token == "prev-run:R47_MAKER_TMF:1"


def test_a_quarantine_written_after_the_snapshot_is_still_restored(tmp_path: Path) -> None:
    """The snapshot is taken before this run fences the previous one.

    An engine still alive during startup can latch a strategy in that window.
    Hydrating only the pre-fence snapshot would resume a strategy the operator
    had just stopped.
    """
    state = tmp_path / "runtime_state.json"
    pre_fence = {"platform": {"manual_rearm_required": False, "reason": None}, "strategies": {}}
    _write_state(state, pre_fence)

    # ...the other engine latches a strategy while this one is constructing.
    _write_state(state, _latched(strategy_id="OTHER", token="other-run:OTHER:2"))

    governor = StrategyHealthGovernor()
    restored = governor.restore_persisted_quarantines(snapshot=pre_fence, state_path=state)

    assert restored == ["OTHER"]
    assert governor.quarantine_token("OTHER") == "other-run:OTHER:2"


def test_a_latch_only_in_the_pre_fence_snapshot_is_not_dropped(tmp_path: Path) -> None:
    """The merge is a union, not a replacement.

    A cleared-looking fresh read must not be able to release a latch the
    validated snapshot saw.
    """
    state = tmp_path / "runtime_state.json"
    _write_state(state, {"platform": {"manual_rearm_required": False, "reason": None}, "strategies": {}})

    governor = StrategyHealthGovernor()
    restored = governor.restore_persisted_quarantines(snapshot=_latched(), state_path=state)

    assert restored == [_SID]


def test_an_unreadable_reread_applies_the_validated_snapshot_instead_of_crashing(
    tmp_path: Path,
) -> None:
    """Raising here would put a fail-closed read back after the lease.

    That is the crash loop the early-read split exists to avoid, and it is
    unnecessary: the supplied snapshot is itself a validated reading, so no
    latch is lost by using it.
    """
    state = tmp_path / "runtime_state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{ this is not json", encoding="utf-8")

    governor = StrategyHealthGovernor()
    restored = governor.restore_persisted_quarantines(snapshot=_latched(), state_path=state)

    assert restored == [_SID]
    assert governor.is_quarantined(_SID) is True


# --- Review round 3: the same fail-open, in the places round 2 did not look --


@pytest.mark.parametrize("flag", [None, "", 0, 1, "true"])
def test_a_platform_latch_with_a_non_boolean_flag_refuses(tmp_path: Path, flag: object) -> None:
    """Round 2 fixed the strategy half of this and left the platform half.

    ``normalize_state`` ``setdefault``s a damaged ``manual_rearm_required`` to
    ``False``, so a boot came up NORMAL holding a HALT latch it could not read.
    Both sections of one document need the check.
    """
    state = tmp_path / "runtime_state.json"
    _write_state(state, {"platform": {"manual_rearm_required": flag, "reason": "storm"}, "strategies": {}})

    with pytest.raises(RuntimeStateUnreadable):
        ManualRearmGate(state_path=state).snapshot()


def test_a_platform_section_missing_its_latch_field_refuses(tmp_path: Path) -> None:
    state = tmp_path / "runtime_state.json"
    _write_state(state, {"platform": {}, "strategies": {}})

    with pytest.raises(RuntimeStateUnreadable):
        ManualRearmGate(state_path=state).snapshot()


def test_a_clear_recorded_after_the_snapshot_supersedes_the_stale_latch(tmp_path: Path) -> None:
    """An operator authorized the clear during startup; do not resurrect it.

    Merging only the *latched* set could not represent "this run watched it get
    cleared". The stale latch won, the restore did not rewrite the record, and
    memory said quarantined while disk said no re-arm required -- a split brain
    the CLI refuses to re-arm out of.
    """
    state = tmp_path / "runtime_state.json"
    cleared = {
        "platform": {"manual_rearm_required": False, "reason": None},
        "strategies": {_SID: {"manual_rearm_required": False, "reason": None}},
    }
    _write_state(state, cleared)

    governor = StrategyHealthGovernor()
    restored = governor.restore_persisted_quarantines(snapshot=_latched(), state_path=state)

    assert restored == []
    assert governor.is_quarantined(_SID) is False


def test_a_strategy_the_fresh_read_never_mentions_keeps_its_snapshot_latch(tmp_path: Path) -> None:
    """Guard the fix above: "not mentioned" is not "cleared"."""
    state = tmp_path / "runtime_state.json"
    _write_state(state, {"platform": {"manual_rearm_required": False, "reason": None}, "strategies": {}})

    governor = StrategyHealthGovernor()

    assert governor.restore_persisted_quarantines(snapshot=_latched(), state_path=state) == [_SID]


@pytest.mark.parametrize("token", [123, 0.5, [], {}, True])
def test_a_present_but_malformed_token_is_corrupt_not_legacy(tmp_path: Path, token: object) -> None:
    """Treating it as legacy produced a latch nothing could ever clear.

    Restore minted a live token; ``_persist_legacy_tokens`` saw the existing
    truthy value and declined to overwrite it. Disk kept ``123``, memory held
    the new token, the CLI rejected the disk value and the governor rejected
    the CLI's -- repeating every restart while logging migration success.
    """
    state = tmp_path / "runtime_state.json"
    payload = _latched()
    payload["strategies"][_SID]["quarantine_token"] = token
    _write_state(state, payload)

    governor = StrategyHealthGovernor()
    with pytest.raises(StrategyQuarantineStateCorrupt):
        governor.restore_persisted_quarantines(state_path=state)


def test_an_empty_string_token_is_corrupt_not_legacy(tmp_path: Path) -> None:
    """The falsey half of the same bug: it would have been silently re-minted."""
    state = tmp_path / "runtime_state.json"
    payload = _latched()
    payload["strategies"][_SID]["quarantine_token"] = ""
    _write_state(state, payload)

    governor = StrategyHealthGovernor()
    with pytest.raises(StrategyQuarantineStateCorrupt):
        governor.restore_persisted_quarantines(state_path=state)


# --- Review round 3 ---------------------------------------------------------


def test_a_second_start_adopts_the_token_the_first_one_committed(tmp_path: Path) -> None:
    """Two starts migrating the same tokenless latch must not split its identity.

    Both mint before either takes the lock. The loser used to keep its own
    token while disk kept the winner's, so the CLI named one and the governor
    accepted only the other -- a quarantine no operator could clear.
    """
    state = tmp_path / "runtime_state.json"
    legacy = _latched()
    del legacy["strategies"][_SID]["quarantine_token"]
    _write_state(state, legacy)

    first = StrategyHealthGovernor()
    first.restore_persisted_quarantines(state_path=state)
    committed = first.quarantine_token(_SID)
    assert committed

    second = StrategyHealthGovernor()
    second.restore_persisted_quarantines(state_path=state)

    assert second.quarantine_token(_SID) == committed


@pytest.mark.parametrize("snapshot", [None, [], "latched", 3, {"strategies": None}])
def test_a_malformed_supplied_snapshot_refuses(snapshot: object) -> None:
    """A directly supplied snapshot bypasses the strict file reader.

    Both a non-object snapshot and a present-null ``strategies`` used to land
    on the cold-start return, so the one path that skips ``read_state_strict``
    was also the one path with no fail-closed check.
    """
    with pytest.raises(StrategyQuarantineStateCorrupt):
        parse_persisted_quarantines(snapshot)


def test_a_supplied_snapshot_with_no_strategies_key_is_still_a_cold_start() -> None:
    """``read_state_strict`` never hands one out for a document that exists.

    Absence here means "the caller had nothing to say", not "the file lost a
    section" -- that case is rejected before it can reach this function.
    """
    assert parse_persisted_quarantines({"platform": {"manual_rearm_required": False}}) == {}
