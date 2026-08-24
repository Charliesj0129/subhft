"""The strategy re-arm must reach the live governor, not just a JSON file.

Before this was wired, ``hft ops rearm-strategy`` was a write-only loop. The
quarantine that actually gates dispatch is
:attr:`StrategyHealthGovernor._quarantined`, an in-memory dict;
:meth:`ManualRearmGate.rearm_strategy` only cleared a flag in
``runtime_state.json``; and nothing read that flag back --
``_consume_platform_rearm_request`` polls the ``platform`` section only, and
``StrategyHealthGovernor.rearm`` had no production caller at all. The operator
command reported success and changed nothing, so an engine restart was the only
real remedy.

That mattered in production: ``R47_MAKER_TMF`` was quarantined at
2026-08-23T14:18:20Z by one rejected intent and emitted no alpha decision for the
following 22 h, while both ``StrategyQuarantineActive`` and
``ManualRearmRequired`` paged continuously.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hft_platform.ops.evidence import AutonomyEvidenceWriter
from hft_platform.ops.manual_rearm import ManualRearmGate
from hft_platform.ops.strategy_governor import StrategyHealthGovernor
from hft_platform.services.system import HFTSystem

STRATEGY_ID = "R47_MAKER_TMF"


@pytest.fixture
def rig(tmp_path):
    """A real governor, a real evidence writer, and a real gate on one directory."""
    writer = AutonomyEvidenceWriter(base_dir=tmp_path)
    governor = StrategyHealthGovernor(evidence_writer=writer)
    gate = ManualRearmGate(state_path=tmp_path / "runtime_state.json")

    system = HFTSystem.__new__(HFTSystem)
    system.manual_rearm_gate = gate
    system.strategy_runner = SimpleNamespace(strategy_governor=governor)
    return SimpleNamespace(governor=governor, gate=gate, system=system)


def test_an_operator_rearm_clears_the_live_quarantine(rig):
    """The whole loop: quarantine -> persisted flag -> CLI clear -> live governor."""
    rig.governor.quarantine(STRATEGY_ID, reason="strategy_exception")
    assert rig.governor.is_quarantined(STRATEGY_ID)
    assert rig.gate.requires_manual_rearm("strategy", strategy_id=STRATEGY_ID)

    rig.gate.rearm_strategy(STRATEGY_ID)

    # One supervisor tick is all the operator should have to wait for.
    rig.system._consume_strategy_rearm_requests(rig.gate.snapshot())

    assert not rig.governor.is_quarantined(STRATEGY_ID), (
        "the operator cleared the persisted flag but the live governor still holds "
        "the quarantine, so the strategy stays silent until an engine restart"
    )


def test_a_quarantine_survives_a_tick_when_no_rearm_was_requested(rig):
    """The poll must not clear a quarantine nobody asked to clear."""
    rig.governor.quarantine(STRATEGY_ID, reason="strategy_exception")

    for _ in range(5):
        rig.system._consume_strategy_rearm_requests(rig.gate.snapshot())

    assert rig.governor.is_quarantined(STRATEGY_ID)


def test_a_strategy_missing_from_the_state_file_stays_quarantined(rig):
    """Absent persisted state is not consent -- fail closed.

    If the evidence writer is disabled or its write failed, the strategy is
    quarantined in memory with no entry in the file. Reading that absence as a
    re-arm would let a write failure silently restore a strategy that a real
    exception took down.
    """
    rig.governor.quarantine(STRATEGY_ID, reason="strategy_exception")

    rig.system._consume_strategy_rearm_requests({"platform": {}, "strategies": {}})

    assert rig.governor.is_quarantined(STRATEGY_ID)


def test_a_rearmed_strategy_can_be_quarantined_again(rig):
    """No watermark is needed: a fresh quarantine rewrites the flag to true."""
    rig.governor.quarantine(STRATEGY_ID, reason="strategy_exception")
    rig.gate.rearm_strategy(STRATEGY_ID)
    rig.system._consume_strategy_rearm_requests(rig.gate.snapshot())
    assert not rig.governor.is_quarantined(STRATEGY_ID)

    rig.governor.quarantine(STRATEGY_ID, reason="strategy_exception")
    rig.system._consume_strategy_rearm_requests(rig.gate.snapshot())

    assert rig.governor.is_quarantined(STRATEGY_ID), "the second quarantine was cleared by a stale re-arm flag"


def test_the_tick_is_a_no_op_without_a_runner(rig):
    """Partially constructed systems (tests, early boot) must not raise."""
    rig.system.strategy_runner = None
    rig.system._consume_strategy_rearm_requests({"strategies": {}})

    rig.system.strategy_runner = SimpleNamespace(strategy_governor=None)
    rig.system._consume_strategy_rearm_requests({"strategies": {}})


def test_an_unreadable_state_file_leaves_the_quarantine_alone(rig):
    """A missing or unparseable snapshot must not be read as a re-arm."""
    rig.governor.quarantine(STRATEGY_ID, reason="strategy_exception")

    rig.system._consume_strategy_rearm_requests(None)
    rig.system._consume_strategy_rearm_requests({})

    assert rig.governor.is_quarantined(STRATEGY_ID)
