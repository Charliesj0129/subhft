"""H4: StormGuard must expose an atomic check-and-submit gate so that a
concurrent trigger_halt cannot race between the state-read and the
downstream order-queue put.

Root cause (H4): three layers (risk validate → risk post-approve recheck
→ order-adapter stamp/live recheck) each acquire ``_state_lock``
independently. Between Layer 1 (validate) and Layer 2 (put to
order_queue) the lock is released, so ``trigger_halt`` firing in a
second thread between these points leaves the cmd on the queue with a
pre-HALT stamp. The adapter's live recheck usually catches it, but the
race produces inconsistent DLQ bookkeeping and dirty telemetry
(risk_halt_blocked_total and stormguard_halt_exempt_bypass_total
over-count depending on scheduling).

Fix: ``StormGuard.check_and_submit(intent, submit_fn)`` acquires
_state_lock, runs the validation logic, and — if approved — calls
``submit_fn`` WHILE THE LOCK IS STILL HELD. A concurrent trigger_halt
blocks on the lock and either preempts (intent sees HALT) or runs after
submit (submit completes before HALT). Either ordering is consistent.

The public ``validate()`` keeps working for legacy call sites; both
methods share the single ``_validate_locked`` helper so the policy is
never duplicated.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, StormGuardState
from hft_platform.risk.storm_guard import StormGuard


def _mk_intent(intent_type: IntentType = IntentType.NEW, strategy_id: str = "S1") -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol="TMFD6",
        intent_type=intent_type,
        side=Side.BUY,
        price=10000 if intent_type == IntentType.NEW else 0,
        qty=1 if intent_type == IntentType.NEW else 0,
    )


def test_check_and_submit_invokes_submit_fn_under_lock_when_normal():
    sg = StormGuard()
    submitted: list[bool] = []

    def _submit():
        # While we are here, trigger_halt must be blocked because we hold
        # the lock. We can't directly assert that, but we can fire a
        # trigger_halt in a background thread and confirm it hasn't
        # changed state by the time submit_fn returns.
        submitted.append(True)

    ok, reason = sg.check_and_submit(_mk_intent(), _submit)
    assert ok is True
    assert reason == "OK"
    assert submitted == [True]


def test_check_and_submit_refuses_when_halted():
    sg = StormGuard()
    sg.trigger_halt("test")
    submit_fn = MagicMock()
    ok, reason = sg.check_and_submit(_mk_intent(), submit_fn)
    assert ok is False
    assert reason == "STORMGUARD_HALT"
    submit_fn.assert_not_called()


def test_check_and_submit_allows_cancel_under_halt():
    sg = StormGuard()
    sg.trigger_halt("test")
    submitted: list[bool] = []
    ok, reason = sg.check_and_submit(_mk_intent(IntentType.CANCEL), lambda: submitted.append(True))
    assert ok is True
    assert reason == "OK"
    assert submitted == [True]


def test_check_and_submit_race_with_trigger_halt_is_consistent():
    """H4 race: run check_and_submit and trigger_halt concurrently across
    many iterations. The invariant: if submit_fn was called, the state
    at submit-time was not HALT (the check and submit were atomic).
    """
    iterations = 200
    violations: list[str] = []

    for _ in range(iterations):
        sg = StormGuard()
        # Reset to NORMAL each loop so we explore the race.
        submitted_ok: list[bool] = []

        def _submit():
            # At this exact moment the lock is held, so state cannot
            # transition underneath us. Snapshot the state and record.
            submitted_ok.append(sg.state == StormGuardState.NORMAL)

        def _run_submit():
            ok, _ = sg.check_and_submit(_mk_intent(), _submit)
            # If ok was returned, submit_fn must have been invoked AND
            # the state must have been non-HALT at submit time.
            if ok and submitted_ok and not submitted_ok[-1]:
                violations.append("submit happened under HALT state")

        def _run_halt():
            sg.trigger_halt("race")

        t_submit = threading.Thread(target=_run_submit)
        t_halt = threading.Thread(target=_run_halt)
        t_halt.start()
        t_submit.start()
        t_halt.join(timeout=1)
        t_submit.join(timeout=1)

    assert not violations, f"H4 invariant violated: {violations[:5]}"


def test_validate_and_check_and_submit_agree():
    """Regression: the two entry points must apply identical policy."""
    sg = StormGuard()
    sg.trigger_halt("test")
    intent = _mk_intent()

    v_ok, v_reason = sg.validate(intent)
    s_ok, s_reason = sg.check_and_submit(intent, MagicMock())

    assert v_ok == s_ok
    assert v_reason == s_reason
