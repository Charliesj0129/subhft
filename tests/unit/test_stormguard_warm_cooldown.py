"""Tests for WARM de-escalation cooldown (P4 — 2026-04-28).

Production bug discovered live on 2026-04-28: NORMAL<->WARM flap cycles on
EXFE6 fired WARM->NORMAL after only ~5 seconds despite the de-escalation
log line claiming ``cooldown_s=30.0, threshold=5``. Root cause: the
``cooldown_ok`` branch in ``StormGuard.update()`` for ``state==WARM`` fell
through to the ``else: cooldown_ok = True`` clause, so de-escalation was
gated only by the ``_de_escalate_threshold`` (5 consecutive clears at 1Hz
update cadence -> ~5s). The log message printed ``_storm_cooldown_s`` even
though the storm cooldown was never checked.

These tests pin the corrected behaviour:

1. WARM->NORMAL respects ``_warm_cooldown_s`` (must wait the full cooldown).
2. The de-escalation log line reports the *actually applicable* cooldown
   (warm vs storm vs halt), not always the storm value.
3. Drift-burst (LOB) WARM entries also arm ``_warm_entry_ts``.
4. Toxicity hysteresis re-arms WARM cooldown while toxicity stays in the
   hold band (between ``_warm_toxicity_exit`` and ``_warm_toxicity_entry``)
   — production toxicity 0.503/0.509/0.514 case.
5. STORM/HALT cooldown behaviour is unchanged (regression guard).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from hft_platform.risk.storm_guard import RiskThresholds, StormGuard, StormGuardState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _ToxicityResult:
    burst_detected: bool
    toxicity_score: float
    burst_event: object | None = None


class _StubDetector:
    """Minimal DriftBurstDetector stand-in returning a canned toxicity score."""

    def __init__(self, score: float, burst: bool = False) -> None:
        self._score = score
        self._burst = burst

    def evaluate(self, *_args, **_kwargs) -> _ToxicityResult:
        return _ToxicityResult(burst_detected=self._burst, toxicity_score=self._score)

    def set(self, score: float, burst: bool = False) -> None:
        self._score = score
        self._burst = burst


def _make_sg(
    warm_cooldown_s: float = 30.0,
    storm_cooldown_s: float = 30.0,
    de_n: int = 5,
    warm_reescalation_cd: float = 300.0,
    detector: _StubDetector | None = None,
) -> StormGuard:
    env = {
        "HFT_STORMGUARD_WARM_COOLDOWN_S": str(warm_cooldown_s),
        "HFT_STORMGUARD_STORM_COOLDOWN_S": str(storm_cooldown_s),
        "HFT_STORMGUARD_DE_ESCALATE_N": str(de_n),
        "HFT_STORMGUARD_WARM_REESCALATION_COOLDOWN_S": str(warm_reescalation_cd),
    }
    with patch.dict(os.environ, env):
        sg = StormGuard(thresholds=RiskThresholds(), drift_burst_detector=detector)
    sg.metrics = MagicMock()
    sg.metrics.stormguard_mode = MagicMock()
    sg.metrics.stormguard_mode.labels.return_value = MagicMock()
    sg.metrics.stormguard_transitions_total = MagicMock()
    sg.metrics.stormguard_transitions_total.labels.return_value = MagicMock()
    return sg


# ---------------------------------------------------------------------------
# 1. WARM->NORMAL respects _warm_cooldown_s
# ---------------------------------------------------------------------------


def test_warm_does_not_deescalate_before_cooldown_elapses():
    """REGRESSION: production 2026-04-28 — WARM->NORMAL fired after ~5s
    despite logged cooldown=30s. After fix, WARM must hold for the full
    ``_warm_cooldown_s`` before de-escalating."""
    sg = _make_sg(warm_cooldown_s=30.0, de_n=5)
    # Enter WARM via latency path (deterministic, no detector needed).
    state = sg.update(latency_us=sg.thresholds.latency_warm_us + 1)
    assert state == StormGuardState.WARM
    assert sg._warm_entry_ts > 0  # P4: must be armed on WARM entry

    # Simulate 5 clear updates immediately (1Hz cadence in production).
    # Pre-fix this would have flipped to NORMAL on the 5th. Post-fix the
    # cooldown gate keeps state at WARM.
    for _ in range(5):
        state = sg.update(latency_us=0)
    assert state == StormGuardState.WARM, "WARM must hold while _warm_cooldown_s has not elapsed (production bug)"


def test_warm_deescalates_after_cooldown_elapsed_and_n_clears():
    """After cooldown elapses, N consecutive clears finally fire WARM->NORMAL."""
    sg = _make_sg(warm_cooldown_s=30.0, de_n=3)
    sg.update(latency_us=sg.thresholds.latency_warm_us + 1)
    assert sg.state == StormGuardState.WARM

    # Fast-forward _warm_entry_ts past the cooldown window.
    sg._warm_entry_ts = sg._warm_entry_ts - 31.0

    # 1st and 2nd clears: still WARM (de_n=3, threshold not yet reached).
    sg.update(latency_us=0)
    assert sg.state == StormGuardState.WARM
    sg.update(latency_us=0)
    assert sg.state == StormGuardState.WARM
    # 3rd clear: meets de_n threshold, transitions to NORMAL.
    sg.update(latency_us=0)
    assert sg.state == StormGuardState.NORMAL


def test_warm_short_cooldown_allows_quick_recovery():
    """With cooldown=0.0 the prior behaviour (n-clears only) is preserved
    — operator opt-out for sim/replay scenarios that require fast cycling."""
    sg = _make_sg(warm_cooldown_s=0.0, de_n=2)
    sg.update(latency_us=sg.thresholds.latency_warm_us + 1)
    assert sg.state == StormGuardState.WARM
    sg.update(latency_us=0)
    assert sg.state == StormGuardState.WARM
    sg.update(latency_us=0)
    assert sg.state == StormGuardState.NORMAL


# ---------------------------------------------------------------------------
# 2. De-escalation log line reports the right cooldown
# ---------------------------------------------------------------------------


def test_warm_deescalation_log_reports_warm_cooldown_not_storm():
    """Pre-fix the log printed ``_storm_cooldown_s=30.0`` even on WARM
    de-escalation (misleading operators). Post-fix, ``cooldown_s`` matches
    ``_warm_cooldown_s``."""
    sg = _make_sg(warm_cooldown_s=42.0, storm_cooldown_s=999.0, de_n=1)
    sg.update(latency_us=sg.thresholds.latency_warm_us + 1)
    assert sg.state == StormGuardState.WARM
    sg._warm_entry_ts = sg._warm_entry_ts - 50.0  # past cooldown

    with patch("hft_platform.risk.storm_guard.logger") as mock_log:
        sg.update(latency_us=0)
    assert sg.state == StormGuardState.NORMAL

    # Find the de-escalation log call and assert it shows WARM cooldown.
    deesc_calls = [
        c for c in mock_log.info.call_args_list if c.args and c.args[0] == "StormGuard de-escalated after hysteresis"
    ]
    assert deesc_calls, "expected de-escalation log entry"
    kwargs = deesc_calls[0].kwargs
    assert kwargs["from_state"] == "WARM"
    assert kwargs["to_state"] == "NORMAL"
    assert kwargs["cooldown_s"] == 42.0, (
        f"WARM de-escalation log must report _warm_cooldown_s; got {kwargs['cooldown_s']!r}"
    )


# ---------------------------------------------------------------------------
# 3. Drift-burst (LOB) WARM entries arm _warm_entry_ts too
# ---------------------------------------------------------------------------


def test_drift_burst_warm_entry_arms_warm_cooldown():
    """update_with_lob() WARM entry must also set _warm_entry_ts so the
    de-escalation cooldown applies — regardless of which path raised WARM."""
    detector = _StubDetector(score=0.0)
    sg = _make_sg(warm_cooldown_s=30.0, de_n=1, detector=detector)
    detector.set(score=0.55)
    state = sg.update_with_lob(mid_price_x2=1000, spread_scaled=10, symbol="EXFE6")
    assert state == StormGuardState.WARM
    assert sg._warm_entry_ts > 0, "drift-burst WARM entry must arm _warm_entry_ts (P4)"

    # Drop toxicity to zero (no detector fire) and run update() with clean inputs.
    # Pre-fix this would have de-escalated immediately because _warm_entry_ts
    # was never set; post-fix the cooldown holds WARM.
    detector.set(score=0.0)
    sg.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
    assert sg.state == StormGuardState.WARM, (
        "WARM must hold while _warm_cooldown_s has not elapsed even when entry was via drift-burst"
    )


# ---------------------------------------------------------------------------
# 4. Toxicity hysteresis band re-arms WARM while score stays above exit
# ---------------------------------------------------------------------------


def test_toxicity_hysteresis_holds_warm_in_band():
    """Once in WARM, toxicity scores in the hold band [exit, entry] re-arm
    ``_warm_entry_ts`` so update()'s de-escalation does not fire while
    toxicity is still above the exit threshold. Models the production
    pathology (0.503/0.509/0.514 hovering just above 0.5)."""
    detector = _StubDetector(score=0.0)
    sg = _make_sg(warm_cooldown_s=30.0, de_n=1, detector=detector)

    # 1. Enter WARM at toxicity 0.55.
    detector.set(score=0.55)
    sg.update_with_lob(mid_price_x2=1000, spread_scaled=10, symbol="EXFE6")
    assert sg.state == StormGuardState.WARM
    initial_ts = sg._warm_entry_ts

    # 2. Drop toxicity into the hold band (between exit=0.4 and entry=0.5).
    detector.set(score=0.45)
    state = sg.update_with_lob(mid_price_x2=1000, spread_scaled=10, symbol="EXFE6")
    assert state == StormGuardState.WARM, "hold-band toxicity must keep WARM"
    assert sg._warm_entry_ts >= initial_ts, "hold-band toxicity must re-arm _warm_entry_ts"


def test_toxicity_below_exit_lets_warm_deescalate_after_cooldown():
    """When toxicity drops cleanly below the exit threshold, the WARM
    cooldown clock starts ticking. After it elapses + N clears, WARM
    finally de-escalates to NORMAL."""
    detector = _StubDetector(score=0.0)
    sg = _make_sg(warm_cooldown_s=30.0, de_n=1, detector=detector)

    detector.set(score=0.55)
    sg.update_with_lob(mid_price_x2=1000, spread_scaled=10, symbol="EXFE6")
    assert sg.state == StormGuardState.WARM

    # Toxicity falls fully below exit threshold — no re-arm.
    detector.set(score=0.1)
    sg.update_with_lob(mid_price_x2=1000, spread_scaled=10, symbol="EXFE6")
    assert sg.state == StormGuardState.WARM  # cooldown still active

    # Fast-forward past warm cooldown.
    sg._warm_entry_ts = sg._warm_entry_ts - 31.0
    sg.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
    assert sg.state == StormGuardState.NORMAL


# ---------------------------------------------------------------------------
# 5. Regression guard: STORM/HALT cooldowns unchanged
# ---------------------------------------------------------------------------


def test_storm_cooldown_path_unchanged():
    """Pre-existing STORM cooldown semantics unaffected by P4 fix."""
    sg = _make_sg(warm_cooldown_s=999.0, storm_cooldown_s=30.0, de_n=1)
    sg.update(feed_gap_s=sg.thresholds.feed_gap_storm_s + 0.1)
    assert sg.state == StormGuardState.STORM
    # 1 clear before cooldown elapsed: stays STORM (cooldown gate).
    sg.update()
    assert sg.state == StormGuardState.STORM
    # Fast-forward past STORM cooldown.
    sg._storm_entry_ts = sg._storm_entry_ts - 31.0
    sg.update()
    assert sg.state == StormGuardState.NORMAL


def test_halt_cooldown_path_unchanged():
    """Pre-existing HALT cooldown semantics unaffected by P4 fix."""
    sg = _make_sg(warm_cooldown_s=999.0, storm_cooldown_s=999.0, de_n=1)
    sg._halt_cooldown_s = 30.0
    sg.update(drawdown_bps=-200)
    assert sg.state == StormGuardState.HALT
    sg.update()
    assert sg.state == StormGuardState.HALT
    sg._halt_entry_ts = sg._halt_entry_ts - 31.0
    sg.update()
    assert sg.state == StormGuardState.NORMAL


def test_warm_entry_ts_resets_after_full_deescalation():
    """After WARM->NORMAL, ``_warm_entry_ts`` is reset to 0.0 so the next
    WARM entry gets a fresh cooldown clock (mirrors _storm_entry_ts reset)."""
    sg = _make_sg(warm_cooldown_s=30.0, de_n=1)
    sg.update(latency_us=sg.thresholds.latency_warm_us + 1)
    assert sg.state == StormGuardState.WARM
    sg._warm_entry_ts = sg._warm_entry_ts - 31.0
    sg.update(latency_us=0)
    assert sg.state == StormGuardState.NORMAL
    assert sg._warm_entry_ts == 0.0, "_warm_entry_ts must reset after de-escalation so next WARM gets fresh cooldown"
