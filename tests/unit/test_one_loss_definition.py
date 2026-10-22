"""B4a: one definition of a loss, and StormGuard grades health only.

Before this batch three things judged money: the daily-loss validator (the day's
net change), its 40%-of-peak rule, and StormGuard's drawdown ladder (realised
give-back from the day's peak over a 200,000 NTD reference: 1,000 / 2,000 /
4,000 NTD for WARM / STORM / HALT). On 2026-10-22 16:03Z the last two stopped R47
with the day 1,270 NTD up: a peak of 3,760 and a 2,490 give-back.

DriftBurst, a toxicity statistic about the book, could also reach STORM and HALT
on its own. It is now a CAUTION (WARM) signal.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, StormGuardState
from hft_platform.risk.storm_guard import RiskThresholds, StormGuard
from hft_platform.risk.validators import DailyLossLimitValidator

_REPO = Path(__file__).resolve().parents[2]
_SCALE = 10_000


def _ntd(n: int) -> int:
    return n * _SCALE


def _prod_like_validator() -> DailyLossLimitValidator:
    """The prod intraday block as it stood on 2026-10-22, peak keys included.

    Keeping the removed keys here is deliberate: an old config must not bring
    the rule back.
    """
    cfg = {
        "global_defaults": {"max_daily_loss": 500_000_000},
        "intraday_pnl": {
            "scope": "global",
            "soft_limit_ntd": 5000,  # out of the way: this test is about the peak rule
            "hard_limit_ntd": 15000,
            "peak_drawdown_pct": 0.40,
            "soft_recovery_ntd": 300,
            "drawdown_recovery_pct": 0.20,
            "soft_limit_cooldown_s": 60,
            "peak_drawdown_min_peak_ntd": 3500,
            "price_scale": _SCALE,
        },
    }
    return DailyLossLimitValidator(cfg, None)


def _intent() -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="R47",
        symbol="TMFK6",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=22_000 * _SCALE,
        qty=1,
    )


def _guard(**kwargs) -> StormGuard:
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        return StormGuard(**kwargs)


class TestTheDailyLossValidatorHasNoPeakRule:
    def test_the_2026_10_22_giveback_is_not_a_stop(self) -> None:
        v = _prod_like_validator()
        v.record_pnl("R47", _ntd(3760))
        assert v.check(_intent())[0] is True
        v.record_pnl("R47", _ntd(-2490))  # +1,270 on the day

        ok, reason = v.check(_intent())

        assert ok is True, reason
        assert v.halt_triggered is False

    def test_a_persisted_peak_drawdown_latch_restores_released(self) -> None:
        v = _prod_like_validator()
        state = v.export_state()
        state.update(
            halt_triggered=True,
            halt_reason="PEAK_DRAWDOWN",
            peak_drawdown_halt_ts_ns=1,
            peak_pnl_scaled=_ntd(3760),
        )
        after = _prod_like_validator()

        assert after.restore_state(state) is True
        assert after.halt_triggered is False
        assert after.halt_reason == ""


class TestStormGuardReadsNoPnl:
    def test_update_has_no_drawdown_input(self) -> None:
        assert "drawdown_bps" not in inspect.signature(StormGuard.update).parameters
        assert not hasattr(RiskThresholds(), "halt_drawdown_bps")

    def test_no_health_input_reaches_halt(self) -> None:
        sg = _guard()
        sg.update(latency_us=10_000_000, order_rtt_us=60_000_000, feed_gap_s=3600.0)
        assert sg.state == StormGuardState.STORM

    def test_a_trigger_halt_reason_survives_a_clean_tick(self) -> None:
        sg = _guard()
        sg.trigger_halt("DAILY_LOSS_LIMIT_EXCEEDED")
        sg.update()
        assert sg.state == StormGuardState.HALT
        assert sg.state_reason == "DAILY_LOSS_LIMIT_EXCEEDED"


class TestDriftBurstIsCaution:
    @pytest.mark.parametrize("toxicity, burst", [(0.85, False), (0.95, True)], ids=["storm_band", "halt_band"])
    def test_drift_burst_is_capped_at_warm(self, toxicity: float, burst: bool) -> None:
        detector = MagicMock()
        detector.evaluate.return_value = MagicMock(burst_detected=burst, toxicity_score=toxicity, burst_event=None)
        on_halt = MagicMock()
        sg = _guard(drift_burst_detector=detector, on_halt_callback=on_halt)

        state = sg.update_with_lob(mid_price_x2=44_000 * _SCALE, spread_scaled=_SCALE, symbol="TMFK6")

        assert state == StormGuardState.WARM
        ok, reason = sg.validate(_intent())
        assert ok is True, reason
        on_halt.assert_not_called()


class TestConfigCarriesNoPeakKeys:
    @pytest.mark.parametrize("path", ["config/base/strategy_limits.yaml", "config/env/prod/strategy_limits.yaml"])
    def test_peak_keys_are_gone(self, path: str) -> None:
        intraday = yaml.safe_load((_REPO / path).read_text(encoding="utf-8"))["intraday_pnl"]
        for key in ("peak_drawdown_pct", "drawdown_recovery_pct", "peak_drawdown_min_peak_ntd"):
            assert key not in intraday, f"{path}: {key} is read by nothing"
