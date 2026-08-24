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
