"""The operator re-arm must reach the live governor -- and nothing else may.

Four rounds of dual review on 2026-08-24/25 falsified three successive designs
here. Each test below pins a specific way one of them failed open, so the
channel cannot regress into any of them.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

from hft_platform.ops import rearm_requests
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
    return SimpleNamespace(governor=governor, gate=gate, system=system, writer=writer, base=tmp_path)


def tick(rig) -> None:
    asyncio.run(rig.system._consume_strategy_rearm_requests())


# --- the behaviour ------------------------------------------------------------


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


def test_a_rearmed_strategy_can_be_quarantined_again(rig):
    rig.governor.quarantine(STRATEGY, reason="first")
    rig.gate.rearm_strategy(STRATEGY)
    tick(rig)
    assert not rig.governor.is_quarantined(STRATEGY)

    rig.governor.quarantine(STRATEGY, reason="second")
    rig.gate.rearm_strategy(STRATEGY)
    tick(rig)
    assert not rig.governor.is_quarantined(STRATEGY)


def test_the_tick_is_a_no_op_without_a_runner(rig):
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rig.gate.rearm_strategy(STRATEGY)
    rig.system.strategy_runner = None

    tick(rig)

    assert rig.governor.is_quarantined(STRATEGY)


def test_a_rearm_records_a_normal_transition(rig):
    """Recovery must be reconstructable; the timeline used to end at QUARANTINED."""
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rig.gate.rearm_strategy(STRATEGY)
    tick(rig)

    timeline = (rig.writer._ensure_session_dir() / "state_timeline.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in timeline.splitlines() if line.strip()]
    assert records[-1]["mode"] == "NORMAL"
    assert records[-1]["manual_rearm_required"] is False


def test_a_rearm_clears_the_persisted_flag(rig):
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rig.gate.rearm_strategy(STRATEGY)
    tick(rig)

    assert rig.gate.snapshot()["strategies"][STRATEGY]["manual_rearm_required"] is False


# --- round 1: a level-triggered flag is not an authorization ------------------


def test_a_stale_cleared_flag_does_not_rearm_a_later_quarantine(rig):
    """A cleared flag means "nothing to do", never "an operator authorized this"."""
    rig.governor.quarantine(STRATEGY, reason="first")
    rig.gate.rearm_strategy(STRATEGY)
    tick(rig)
    assert not rig.governor.is_quarantined(STRATEGY)

    rig.governor.quarantine(STRATEGY, reason="second")
    tick(rig)

    assert rig.governor.is_quarantined(STRATEGY)


def test_a_quarantine_whose_persist_did_not_land_is_not_rearmed(rig):
    """A quarantine whose flag never reached disk must still gate dispatch."""
    rig.governor.quarantine(STRATEGY, reason="first")
    rig.gate.rearm_strategy(STRATEGY)
    tick(rig)

    rig.governor.evidence_writer = None
    rig.governor.quarantine(STRATEGY, reason="second")
    for _ in range(10):
        tick(rig)

    assert rig.governor.is_quarantined(STRATEGY)


def test_a_request_is_single_use(rig):
    rig.governor.quarantine(STRATEGY, reason="first")
    rig.gate.rearm_strategy(STRATEGY)
    tick(rig)
    assert not rig.governor.is_quarantined(STRATEGY)

    rig.governor.quarantine(STRATEGY, reason="second")
    tick(rig)

    assert rig.governor.is_quarantined(STRATEGY), "a consumed request must not replay"


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


# --- round 3/4: an authorization must never release a different quarantine ----


def test_a_request_naming_another_quarantine_is_refused_and_retired(rig):
    rig.governor.quarantine(STRATEGY, reason="first")
    rig.gate.rearm_strategy(STRATEGY)

    # A re-quarantine mints a new token while the request is in flight.
    rig.governor.quarantine(STRATEGY, reason="second")
    live = rig.governor.quarantine_token(STRATEGY)
    tick(rig)

    assert rig.governor.is_quarantined(STRATEGY)
    assert rig.governor.quarantine_token(STRATEGY) == live
    assert rearm_requests.pending(rig.base) == [], "a superseded request must not linger"


def test_the_governor_refuses_a_token_that_is_not_live(rig):
    rig.governor.quarantine(STRATEGY, reason="first")
    assert rig.governor.rearm(STRATEGY, expected_token="someone-elses-token") is False
    assert rig.governor.is_quarantined(STRATEGY)


def test_a_concurrent_requarantine_cannot_be_cleared_by_an_older_authorization(rig):
    """The decision and the removal happen with nothing in between."""
    rig.governor.quarantine(STRATEGY, reason="first")
    token_1 = rig.governor.quarantine_token(STRATEGY)
    rig.governor.quarantine(STRATEGY, reason="second")
    token_2 = rig.governor.quarantine_token(STRATEGY)

    assert token_2 != token_1
    assert rig.governor.rearm(STRATEGY, expected_token=token_1) is False
    assert rig.governor.is_quarantined(STRATEGY)
    assert rig.governor.quarantine_token(STRATEGY) == token_2


def test_a_request_written_before_a_restart_still_clears_the_restored_latch(rig):
    """This test used to assert the opposite, and the docstring said why.

    It read: *a quarantine does not survive a restart, so neither may its
    request* -- and it simulated the restart by constructing a fresh governor
    and nothing else. Once ``restore_persisted_quarantines`` existed that
    simulation stopped being a restart: a real one hydrates the latch and
    reuses the persisted token verbatim, precisely so an authorization the
    operator published before the restart still names the latch it authorizes.
    Retiring it there would destroy a legitimate authorization on every boot,
    and a restart loop would destroy every retry.
    """
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    token = rig.governor.quarantine_token(STRATEGY)
    rig.gate.rearm_strategy(STRATEGY)

    # A restart: fresh governor, then the boot restore the engine performs.
    fresh = StrategyHealthGovernor(evidence_writer=rig.writer)
    assert fresh.restore_persisted_quarantines(state_path=rig.gate.state_path) == [STRATEGY]
    assert fresh.quarantine_token(STRATEGY) == token, "the restore must reuse the persisted token"
    rig.system.strategy_runner = SimpleNamespace(strategy_governor=fresh)

    tick(rig)

    assert not fresh.is_quarantined(STRATEGY), "the pre-restart authorization must still apply"
    assert rearm_requests.pending(rig.base) == []


def test_a_request_for_another_engines_quarantine_is_left_alone(rig):
    """The request directory is shared state, and two engines can scan it.

    ``SystemBootstrapper._check_session_ownership`` is advisory -- it logs and
    ``build()`` continues -- so engine A can be consuming while engine B holds
    the quarantine the operator actually authorized. A token is
    ``run_id:strategy_id:seq`` with ``run_id = pid-uuid4``, so A can see that
    the token was never its to judge. Unlinking it would destroy B's
    authorization with no error anywhere the operator would look.
    """
    other = StrategyHealthGovernor(evidence_writer=rig.writer)
    other.quarantine(STRATEGY, reason="the other engine's failure")
    foreign_token = other.quarantine_token(STRATEGY)
    rig.gate.rearm_strategy(STRATEGY)

    # This engine has its own, different, live quarantine for the same strategy.
    rig.governor.quarantine(STRATEGY, reason="our failure")
    ours = rig.governor.quarantine_token(STRATEGY)
    assert ours != foreign_token

    tick(rig)

    assert rig.governor.is_quarantined(STRATEGY), "a foreign token must not clear our latch"
    assert rig.governor.quarantine_token(STRATEGY) == ours
    still = rearm_requests.pending(rig.base)
    assert [r.quarantine_token for r in still] == [foreign_token], "the other engine's request was destroyed"


def test_a_foreign_request_is_reported_once_not_once_per_tick(rig):
    """The scan runs every supervisor tick and a foreign request is never consumed."""
    other = StrategyHealthGovernor(evidence_writer=rig.writer)
    other.quarantine(STRATEGY, reason="the other engine's failure")
    rig.gate.rearm_strategy(STRATEGY)
    rig.governor.quarantine(STRATEGY, reason="our failure")

    seen: list[str] = []
    import structlog

    def _capture(_logger, _name, event_dict):
        seen.append(event_dict.get("event", ""))
        return event_dict

    # Save and restore the exact config rather than ``reset_defaults()``: this
    # process configures structlog in ``utils.logging`` at import, and resetting
    # to structlog's own defaults would leave every later test in this xdist
    # worker running against a different logger. See the lesson recorded for
    # ``patch('mod.time.sleep')`` -- global mutation passes alone and fails in
    # a suite.
    saved = structlog.get_config()
    structlog.configure(processors=[_capture, *saved["processors"]])
    try:
        for _ in range(4):
            tick(rig)
    finally:
        structlog.configure(**saved)

    assert seen.count("strategy_rearm_request_foreign_token") == 1, seen


# --- failure handling ---------------------------------------------------------


def test_an_evidence_failure_does_not_block_an_authorized_recovery(rig):
    """The gate is memory; the record is an audit trail and must not veto it."""

    class _Boom:
        def record_transition(self, **_kwargs):
            raise OSError("No space left on device")

    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rig.gate.rearm_strategy(STRATEGY)
    rig.governor.evidence_writer = _Boom()

    tick(rig)

    assert not rig.governor.is_quarantined(STRATEGY)


def test_a_tick_does_not_raise_when_the_request_dir_is_unreadable(rig, monkeypatch):
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    monkeypatch.setattr(rearm_requests, "pending", lambda _base: (_ for _ in ()).throw(OSError("EACCES")))

    tick(rig)  # must not raise

    assert rig.governor.is_quarantined(STRATEGY)


def test_a_malformed_request_is_skipped_and_left_for_an_operator(rig):
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    directory = rearm_requests.request_dir(rig.base)
    directory.mkdir(parents=True, exist_ok=True)
    bad = directory / "broken.json"
    bad.write_text("{ truncated", encoding="utf-8")

    tick(rig)

    assert rig.governor.is_quarantined(STRATEGY)
    assert bad.exists(), "deleting it would hide the problem"


def test_a_request_without_a_token_is_never_treated_as_authorization(rig):
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    directory = rearm_requests.request_dir(rig.base)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "notoken.json").write_text(json.dumps({"request_id": "r1", "strategy_id": STRATEGY}), encoding="utf-8")

    tick(rig)

    assert rig.governor.is_quarantined(STRATEGY)


# --- the channel itself -------------------------------------------------------


def test_publishing_the_same_request_id_twice_is_refused(rig):
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rearm_requests.publish(rig.base, strategy_id=STRATEGY, quarantine_token="t", request_id="dup")
    with pytest.raises(FileExistsError):
        rearm_requests.publish(rig.base, strategy_id=STRATEGY, quarantine_token="t", request_id="dup")


def test_pending_is_empty_before_any_request_exists(rig):
    assert rearm_requests.pending(rig.base) == []


def test_consuming_a_request_twice_is_harmless(rig):
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    rig.gate.rearm_strategy(STRATEGY)
    (request,) = rearm_requests.pending(rig.base)
    rearm_requests.consume(request)
    rearm_requests.consume(request)
    assert rearm_requests.pending(rig.base) == []


def test_concurrent_publishers_do_not_lose_requests(rig):
    """Write-once files have no read-modify-write, so nothing can be clobbered."""
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    errors: list[BaseException] = []

    def publish(n: int) -> None:
        try:
            rearm_requests.publish(rig.base, strategy_id=STRATEGY, quarantine_token="t", request_id=f"r{n}")
        except BaseException as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=publish, args=(n,)) for n in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert len(rearm_requests.pending(rig.base)) == 12


def test_a_partially_written_request_is_never_visible(rig):
    """The name appears only after a completed write."""
    directory = rearm_requests.request_dir(rig.base)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".half.1234.tmp").write_text('{"request_id": "hal', encoding="utf-8")
    assert rearm_requests.pending(rig.base) == []


def test_the_scan_is_cheap_when_there_is_nothing_to_do(rig):
    """This runs on the event loop every tick."""
    rig.governor.quarantine(STRATEGY, reason="handler_exception")
    started = time.perf_counter()
    for _ in range(200):
        rearm_requests.pending(rig.base)
    per_call_ms = (time.perf_counter() - started) * 1000 / 200
    assert per_call_ms < 1.0, f"empty scan cost {per_call_ms:.3f} ms per tick"
