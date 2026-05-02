"""Regression test for Bug 20 (2026-04-17).

DriftBurstDetector kept stale _last_mid_x2 across TAIFEX session boundary
(13:45 day close → 15:00 night open = 75 min gap). First night-session tick
produced a log-return that dwarfed the intraday diffusion returns in the ring
buffer, driving |T| to saturation and toxicity → 1.000 → false HALT.

Fix: stale_reset_ns (default 5 min) resets detector state when the gap between
consecutive ticks exceeds the threshold.
"""

from __future__ import annotations

from hft_platform.risk.drift_burst_detector import DriftBurstDetector


def _fill_window(d: DriftBurstDetector, base_ts: int) -> None:
    """Push 100 small-diffusion ticks to warm the detector."""
    mid = 1_000_000
    for i in range(110):
        mid += 10 if i % 2 == 0 else -10  # small oscillation
        d.evaluate(mid_price_x2=mid, ts=base_ts + i * 1_000_000)  # 1ms apart


class TestDriftBurstSessionGapReset:
    def test_long_gap_resets_detector(self):
        d = DriftBurstDetector(stale_reset_ns=300_000_000_000)
        t0 = 1_000_000_000_000
        _fill_window(d, t0)
        assert d._count == 100  # warmed

        # 75 min gap (day close → night open)
        t_night = t0 + int(75 * 60 * 1e9)
        result = d.evaluate(mid_price_x2=1_001_000, ts=t_night)

        # After reset, first post-gap tick must not trigger burst
        assert result.toxicity_score == 0.0
        assert not result.burst_detected

    def test_short_gap_preserves_state(self):
        d = DriftBurstDetector(stale_reset_ns=300_000_000_000)
        t0 = 1_000_000_000_000
        _fill_window(d, t0)
        prior_count = d._count

        # 1 min gap — should preserve state
        t_next = t0 + int(60 * 1e9)
        d.evaluate(mid_price_x2=1_000_100, ts=t_next)
        assert d._count == prior_count  # window intact

    def test_stale_reset_disabled_when_zero(self):
        d = DriftBurstDetector(stale_reset_ns=0)
        t0 = 1_000_000_000_000
        _fill_window(d, t0)

        t_later = t0 + int(24 * 3600 * 1e9)  # 1 day
        d.evaluate(mid_price_x2=1_000_100, ts=t_later)
        assert d._count == 100  # no reset

    def test_zero_ts_does_not_trigger_reset(self):
        """Callers that pass ts=0 (legacy/test) must not hit the reset path."""
        d = DriftBurstDetector(stale_reset_ns=300_000_000_000)
        _fill_window(d, 0)  # ts=0 throughout
        assert d._count == 100

    def test_session_open_after_night_close_no_false_halt(self):
        """Simulate: day session oscillation, 75min quiet, then night jump."""
        d = DriftBurstDetector(stale_reset_ns=300_000_000_000, burst_threshold=3.0)
        t0 = 1_000_000_000_000
        _fill_window(d, t0)

        # Night open: 10bp jump (realistic Mini-TAIEX overnight move)
        t_night = t0 + int(75 * 60 * 1e9)
        result = d.evaluate(mid_price_x2=1_001_000, ts=t_night)
        # Must NOT saturate to 1.0 even on a real jump — the detector
        # reset, so first tick seeds _last_mid and returns zero toxicity.
        assert result.toxicity_score < 0.5
