"""WARM re-escalation cooldown prevents flip-flop between NORMAL and WARM
driven by DriftBurst toxicity oscillating near the 0.5 boundary.

Regression for 58/58 escalations observed on TMFE6 night session.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

from hft_platform.risk.storm_guard import RiskThresholds, StormGuard, StormGuardState


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
    cooldown_s: float = 300.0,
    de_n: int = 1,
    warm_cd: float = 300.0,
    warm_cooldown_s: float = 0.0,
) -> tuple[StormGuard, _StubDetector]:
    """Create a StormGuard for re-escalation tests.

    P4 (2026-04-28): ``warm_cooldown_s`` defaults to 0.0 here because these
    tests focus on **re-escalation** suppression after WARM->NORMAL — the
    de-escalation timing itself is exercised by
    ``test_stormguard_warm_cooldown.py``. Setting WARM cooldown to 0 lets
    these tests still de-escalate via the ``de_n`` threshold alone.
    """
    detector = _StubDetector(score=0.0)
    with patch.dict(
        os.environ,
        {
            "HFT_STORMGUARD_STORM_COOLDOWN_S": str(cooldown_s),
            "HFT_STORMGUARD_DE_ESCALATE_N": str(de_n),
            "HFT_STORMGUARD_WARM_REESCALATION_COOLDOWN_S": str(warm_cd),
            "HFT_STORMGUARD_WARM_COOLDOWN_S": str(warm_cooldown_s),
        },
    ):
        sg = StormGuard(thresholds=RiskThresholds(), drift_burst_detector=detector)
    sg.metrics = MagicMock()
    sg.metrics.stormguard_mode = MagicMock()
    sg.metrics.stormguard_mode.labels.return_value = MagicMock()
    sg.metrics.stormguard_transitions_total = MagicMock()
    sg.metrics.stormguard_transitions_total.labels.return_value = MagicMock()
    return sg, detector


def test_warm_escalation_occurs_before_cooldown_armed():
    sg, det = _make_sg(warm_cd=300.0)
    det.set(score=0.51)
    state = sg.update_with_lob(mid_price_x2=1000, spread_scaled=10, symbol="TMFE6")
    assert state == StormGuardState.WARM


def test_warm_reescalation_suppressed_within_cooldown():
    """After WARM→NORMAL de-escalation, next WARM request within cooldown is suppressed."""
    sg, det = _make_sg(cooldown_s=0.0, de_n=1, warm_cd=300.0)

    # 1. Escalate to WARM
    det.set(score=0.55)
    sg.update_with_lob(mid_price_x2=1000, spread_scaled=10, symbol="TMFE6")
    assert sg.state == StormGuardState.WARM

    # 2. De-escalate to NORMAL via update() with clean inputs (1 clear since de_n=1)
    sg.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
    assert sg.state == StormGuardState.NORMAL
    assert sg._warm_deescalation_ts > 0

    # 3. DriftBurst fires again immediately → should be suppressed by cooldown
    det.set(score=0.52)
    state = sg.update_with_lob(mid_price_x2=1000, spread_scaled=10, symbol="TMFE6")
    assert state == StormGuardState.NORMAL, "WARM re-escalation should be suppressed within cooldown"


def test_warm_reescalation_allowed_after_cooldown():
    """After cooldown elapses, WARM escalation resumes."""
    sg, det = _make_sg(cooldown_s=0.0, de_n=1, warm_cd=0.01)  # 10ms cooldown

    det.set(score=0.55)
    sg.update_with_lob(mid_price_x2=1000, spread_scaled=10, symbol="TMFE6")
    sg.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
    assert sg.state == StormGuardState.NORMAL

    import time

    time.sleep(0.02)

    det.set(score=0.53)
    state = sg.update_with_lob(mid_price_x2=1000, spread_scaled=10, symbol="TMFE6")
    assert state == StormGuardState.WARM


def test_storm_escalation_bypasses_warm_cooldown():
    """A higher-severity target (STORM/HALT) must not be suppressed by WARM cooldown."""
    sg, det = _make_sg(cooldown_s=0.0, de_n=1, warm_cd=300.0)

    det.set(score=0.55)
    sg.update_with_lob(mid_price_x2=1000, spread_scaled=10, symbol="TMFE6")
    sg.update(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)
    assert sg.state == StormGuardState.NORMAL

    # Toxicity jumps to STORM range — must NOT be suppressed by WARM cooldown
    det.set(score=0.85)
    state = sg.update_with_lob(mid_price_x2=1000, spread_scaled=10, symbol="TMFE6")
    assert state == StormGuardState.STORM


def test_symbol_forwarded_to_log():
    """Drift-burst escalation log should include the triggering symbol."""
    sg, det = _make_sg()
    det.set(score=0.6)
    with patch("hft_platform.risk.storm_guard.logger") as mock_logger:
        sg.update_with_lob(mid_price_x2=1000, spread_scaled=10, symbol="TMFE6")
    info_calls = [
        c for c in mock_logger.info.call_args_list if c.args and c.args[0] == "StormGuard drift_burst escalation"
    ]
    assert info_calls, "drift_burst escalation log should fire"
    assert info_calls[0].kwargs.get("symbol") == "TMFE6"
