"""The operator re-arm must reach the live governor -- and nothing else may.

Two independent Codex reviews on 2026-08-24 falsified the first version of this
bridge, which keyed off ``manual_rearm_required`` being false. These tests pin
the request/ack protocol that replaced it, and every fail-open they identified.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hft_platform.ops.evidence import AutonomyEvidenceWriter
from hft_platform.ops.manual_rearm import ManualRearmGate
from hft_platform.ops.strategy_governor import StrategyHealthGovernor
from hft_platform.services.system import HFTSystem

STRATEGY = "R47_MAKER_TMF"


@pytest.fixture
def rig(tmp_path):
    writer = AutonomyEvidenceWriter(base_dir=tmp_path)
    governor = StrategyHealthGovernor(evidence_writer=writer)
    gate = ManualRearmGate(state_path=tmp_path / "runtime_state.json")
    system = HFTSystem.__new__(HFTSystem)
    system.manual_rearm_gate = gate
    system.strategy_runner = SimpleNamespace(strategy_governor=governor)
    return SimpleNamespace(governor=governor, gate=gate, system=system, writer=writer)


def tick(rig) -> None:
    rig.system._consume_strategy_rearm_requests(rig.gate.snapshot())


def test_an_operator_rearm_clears_the_live_quarantine(rig):
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    assert rig.governor.is_quarantined(STRATEGY)

    rig.gate.rearm_strategy(STRATEGY)
    tick(rig)

    assert not rig.governor.is_quarantined(STRATEGY)


def test_a_quarantine_survives_a_tick_when_no_rearm_was_requested(rig):
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    tick(rig)
    assert rig.governor.is_quarantined(STRATEGY)


def test_a_strategy_missing_from_the_state_file_stays_quarantined(rig):
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rig.gate.state_path.write_text(json.dumps({"strategies": {}}), encoding="utf-8")
    tick(rig)
    assert rig.governor.is_quarantined(STRATEGY)


def test_the_tick_is_a_no_op_without_a_runner(rig):
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rig.gate.rearm_strategy(STRATEGY)
    rig.system.strategy_runner = None

    tick(rig)

    assert rig.governor.is_quarantined(STRATEGY)


def test_an_unreadable_state_file_leaves_the_quarantine_alone(rig):
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rig.gate.state_path.write_text("{not json", encoding="utf-8")

    rig.system._consume_strategy_rearm_requests(None)

    assert rig.governor.is_quarantined(STRATEGY)


# --- the fail-opens both reviewers found -------------------------------------


def test_a_stale_cleared_flag_does_not_rearm_a_later_quarantine(rig):
    """The P1/high finding: a stale ``false`` is not an authorization.

    Re-arm once, then quarantine again. The first version re-armed the second
    quarantine immediately, because the entry still read
    ``manual_rearm_required: false`` and nothing distinguished that from a
    fresh operator request.
    """
    rig.governor.quarantine(STRATEGY, reason="first")
    rig.gate.rearm_strategy(STRATEGY)
    tick(rig)
    assert not rig.governor.is_quarantined(STRATEGY)

    rig.governor.quarantine(STRATEGY, reason="second")
    tick(rig)

    assert rig.governor.is_quarantined(STRATEGY)


def test_a_rearm_request_is_single_use(rig):
    rig.governor.quarantine(STRATEGY, reason="first")
    rig.gate.rearm_strategy(STRATEGY)
    state = rig.gate.snapshot()
    tick(rig)
    assert not rig.governor.is_quarantined(STRATEGY)

    # Replay the exact pre-consumption snapshot against a new quarantine.
    rig.governor.quarantine(STRATEGY, reason="second")
    rig.system._consume_strategy_rearm_requests(state)

    assert rig.governor.is_quarantined(STRATEGY)


def test_a_request_naming_another_quarantine_is_refused(rig):
    rig.governor.quarantine(STRATEGY, reason="first")
    rig.gate.rearm_strategy(STRATEGY)
    stale = rig.gate.snapshot()

    # A re-quarantine mints a new token while the operator's request is in flight.
    rig.governor.quarantine(STRATEGY, reason="second")
    rig.system._consume_strategy_rearm_requests(stale)

    assert rig.governor.is_quarantined(STRATEGY)


def test_a_rearm_clears_the_persisted_flag_and_consumes_the_request(rig):
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rig.gate.rearm_strategy(STRATEGY)
    tick(rig)

    entry = rig.gate.snapshot()["strategies"][STRATEGY]
    assert entry["manual_rearm_required"] is False
    assert "rearm_request" not in entry


def test_a_rearm_records_a_normal_transition(rig):
    """Recovery must be reconstructable; the timeline used to end at QUARANTINED."""
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rig.gate.rearm_strategy(STRATEGY)
    tick(rig)

    timeline = (rig.writer._ensure_session_dir() / "state_timeline.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in timeline.splitlines() if line.strip()]
    assert records[-1]["mode"] == "NORMAL"
    assert records[-1]["manual_rearm_required"] is False


def test_a_quarantine_without_a_token_cannot_be_rearmed_by_request(rig):
    """Legacy entries fail closed rather than being treated as authorized."""
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    state = rig.gate.snapshot()
    state["strategies"][STRATEGY].pop("quarantine_token", None)
    rig.gate._write_state(state)

    with pytest.raises(ValueError, match="quarantine_token"):
        rig.gate.rearm_strategy(STRATEGY)

    tick(rig)
    assert rig.governor.is_quarantined(STRATEGY)


def test_the_cli_refuses_to_request_a_rearm_for_a_healthy_strategy(rig):
    with pytest.raises(ValueError, match="does not require manual re-arm"):
        rig.gate.rearm_strategy(STRATEGY)


def test_a_rearmed_strategy_can_be_quarantined_again(rig):
    rig.governor.quarantine(STRATEGY, reason="first")
    rig.gate.rearm_strategy(STRATEGY)
    tick(rig)
    assert not rig.governor.is_quarantined(STRATEGY)

    rig.governor.quarantine(STRATEGY, reason="second")
    rig.gate.rearm_strategy(STRATEGY)
    tick(rig)
    assert not rig.governor.is_quarantined(STRATEGY)


def test_a_quarantine_whose_persist_did_not_land_is_not_rearmed(rig):
    """The real shape of the fail-open both reviews named.

    ``quarantine()`` populates ``_quarantined`` first and only then asks the
    evidence writer to persist ``manual_rearm_required: true``. Anything that
    stops that write -- an unwritable autonomy dir, a full disk, or simply a
    supervisor tick landing inside the window -- leaves the previous re-arm's
    ``false`` in the file. The level-triggered version read that as a fresh
    authorization and re-armed a strategy no operator had touched, every tick,
    for as long as the condition lasted.
    """
    rig.governor.quarantine(STRATEGY, reason="first")
    rig.gate.rearm_strategy(STRATEGY)
    tick(rig)
    assert not rig.governor.is_quarantined(STRATEGY)

    # Second quarantine, persist does not land.
    rig.governor.evidence_writer = None
    rig.governor.quarantine(STRATEGY, reason="second")
    assert rig.gate.snapshot()["strategies"][STRATEGY]["manual_rearm_required"] is False

    tick(rig)

    assert rig.governor.is_quarantined(STRATEGY)


def test_a_persist_failure_does_not_make_a_strategy_permanently_rearmable(rig):
    """The same condition, held across many ticks, must never open."""
    rig.governor.quarantine(STRATEGY, reason="first")
    rig.gate.rearm_strategy(STRATEGY)
    tick(rig)

    rig.governor.evidence_writer = None
    rig.governor.quarantine(STRATEGY, reason="second")
    for _ in range(10):
        tick(rig)

    assert rig.governor.is_quarantined(STRATEGY)


# --- the fail-opens the review of the FIX itself found ------------------------


def test_a_system_start_transition_does_not_clear_a_platform_latch(rig, tmp_path):
    """The regression the first draft of this fix introduced.

    ``HFTSystem.run()`` records ``system_start`` as a platform NORMAL transition
    with ``manual_rearm_required=False`` on **every boot**. Projecting any false
    transition onto runtime_state.json therefore released a genuine platform
    latch that no operator had re-armed -- a restart silently clearing a HALT.
    Only an explicit, correlated strategy re-arm may write false.
    """
    rig.writer.record_transition(
        scope="platform",
        mode="PLATFORM_REDUCE_ONLY",
        reason="clickhouse_unhealthy",
    )
    assert rig.gate.snapshot()["platform"]["manual_rearm_required"] is True

    rig.writer.record_transition(
        scope="platform",
        mode="NORMAL",
        reason="system_start",
        manual_rearm_required=False,
    )

    platform = rig.gate.snapshot()["platform"]
    assert platform["manual_rearm_required"] is True
    assert platform["reason"] == "clickhouse_unhealthy"


def test_a_strategy_normal_transition_without_a_request_id_clears_nothing(rig):
    """A recovery must name the request it consumed, or it proves nothing."""
    rig.governor.quarantine(STRATEGY, reason="handler_exception")

    rig.writer.record_transition(
        scope="strategy",
        mode="NORMAL",
        reason="manual_rearm",
        manual_rearm_required=False,
        metadata={"strategy_id": STRATEGY},
    )

    assert rig.gate.snapshot()["strategies"][STRATEGY]["manual_rearm_required"] is True


def test_concurrent_writers_do_not_share_a_temp_file(rig, tmp_path):
    """Engine and CLI write the same file from different processes.

    A shared ``runtime_state.json.tmp`` means whichever renames second either
    moves the other writer's payload or dies with FileNotFoundError.
    """
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rig.gate.rearm_strategy(STRATEGY)

    leftovers = list(tmp_path.glob("runtime_state.json*.tmp"))
    assert leftovers == [], f"temp files must not survive a write: {leftovers}"

    # Both writers must derive distinct temp names, not one shared path.
    assert not (tmp_path / "runtime_state.json.tmp").exists()


def test_a_failed_acknowledgement_leaves_the_quarantine_intact(rig):
    """Persist first, clear second: a write failure must not open the gate."""

    class _Boom:
        def record_transition(self, **_kwargs):
            raise OSError("No space left on device")

    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rig.gate.rearm_strategy(STRATEGY)
    rig.governor.evidence_writer = _Boom()

    tick(rig)

    assert rig.governor.is_quarantined(STRATEGY)


def test_a_failed_acknowledgement_does_not_escape_the_supervisor_tick(rig):
    """An unhandled error here would restart the whole engine mid-recovery."""

    class _Boom:
        def record_transition(self, **_kwargs):
            raise OSError("No space left on device")

    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rig.gate.rearm_strategy(STRATEGY)
    rig.governor.evidence_writer = _Boom()

    for _ in range(3):
        tick(rig)  # must not raise

    assert rig.governor.is_quarantined(STRATEGY)


def test_a_recovered_acknowledgement_applies_on_a_later_tick(rig):
    """Not recording the id on failure is what makes the retry possible."""

    class _Boom:
        def record_transition(self, **_kwargs):
            raise OSError("No space left on device")

    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rig.gate.rearm_strategy(STRATEGY)

    healthy = rig.governor.evidence_writer
    rig.governor.evidence_writer = _Boom()
    tick(rig)
    assert rig.governor.is_quarantined(STRATEGY)

    rig.governor.evidence_writer = healthy
    tick(rig)

    assert not rig.governor.is_quarantined(STRATEGY)
