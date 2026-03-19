"""Tests for hft_platform.alpha.canary_scheduler module."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hft_platform.alpha.canary import CanaryMonitor, CanaryStatus
from hft_platform.alpha.canary_scheduler import CanaryAutoScheduler


def _write_canary_yaml(
    path: Path,
    alpha_id: str = "ofi_mc",
    weight: float = 0.02,
    enabled: bool = True,
    sharpe_oos: float = 1.5,
    sessions_live: int = 0,
    slippage_bps: float = 0.0,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "alpha_id": alpha_id,
        "enabled": enabled,
        "weight": weight,
        "owner": "test",
        "guardrails": {
            "max_live_slippage_bps": 3.0,
            "max_live_drawdown_contribution": 0.02,
            "max_execution_error_rate": 0.01,
        },
        "rollback": {
            "trigger": {
                "live_slippage_bps_gt": 3.0,
                "live_drawdown_contribution_gt": 0.02,
                "execution_error_rate_gt": 0.01,
            },
        },
        "scorecard_snapshot": {"sharpe_oos": sharpe_oos},
        "live_metrics": {
            "slippage_bps": slippage_bps,
            "drawdown_contribution": 0.0,
            "execution_error_rate": 0.0,
            "sessions_live": sessions_live,
        },
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return path


class TestCanaryAutoSchedulerInit:
    def test_defaults(self, tmp_path: Path) -> None:
        monitor = CanaryMonitor(promotions_dir=str(tmp_path))
        scheduler = CanaryAutoScheduler(monitor=monitor)
        assert scheduler.interval == 86400.0
        assert scheduler.dry_run is True

    def test_explicit_overrides(self, tmp_path: Path) -> None:
        monitor = CanaryMonitor(promotions_dir=str(tmp_path))
        scheduler = CanaryAutoScheduler(monitor=monitor, interval_s=60.0, dry_run=False)
        assert scheduler.interval == 60.0
        assert scheduler.dry_run is False

    def test_env_overrides(self, tmp_path: Path) -> None:
        monitor = CanaryMonitor(promotions_dir=str(tmp_path))
        with patch.dict("os.environ", {"HFT_CANARY_AUTO_INTERVAL_S": "120", "HFT_CANARY_AUTO_DRY_RUN": "0"}):
            scheduler = CanaryAutoScheduler(monitor=monitor)
        assert scheduler.interval == 120.0
        assert scheduler.dry_run is False


class TestCanaryAutoSchedulerLifecycle:
    def test_start_and_stop(self, tmp_path: Path) -> None:
        monitor = CanaryMonitor(promotions_dir=str(tmp_path))
        scheduler = CanaryAutoScheduler(monitor=monitor, interval_s=3600.0)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._start_stop(scheduler))
        finally:
            loop.close()

    @staticmethod
    async def _start_stop(scheduler: CanaryAutoScheduler) -> None:
        scheduler.start()
        assert scheduler._task is not None
        assert not scheduler._task.done()
        scheduler.stop()
        assert scheduler._task is None

    def test_start_disabled_when_interval_zero(self, tmp_path: Path) -> None:
        monitor = CanaryMonitor(promotions_dir=str(tmp_path))
        scheduler = CanaryAutoScheduler(monitor=monitor, interval_s=0)
        scheduler.start()
        assert scheduler._task is None

    def test_double_start_is_safe(self, tmp_path: Path) -> None:
        monitor = CanaryMonitor(promotions_dir=str(tmp_path))
        scheduler = CanaryAutoScheduler(monitor=monitor, interval_s=3600.0)

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self._double_start(scheduler))
        finally:
            loop.close()

    @staticmethod
    async def _double_start(scheduler: CanaryAutoScheduler) -> None:
        scheduler.start()
        task1 = scheduler._task
        scheduler.start()  # second start should not replace task
        assert scheduler._task is task1
        scheduler.stop()

    def test_stop_idempotent(self, tmp_path: Path) -> None:
        monitor = CanaryMonitor(promotions_dir=str(tmp_path))
        scheduler = CanaryAutoScheduler(monitor=monitor, interval_s=3600.0)
        # stop without start — should not raise
        scheduler.stop()
        scheduler.stop()


class TestCanaryAutoSchedulerEvaluateAll:
    def test_dry_run_evaluates_but_does_not_apply(self, tmp_path: Path) -> None:
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(promo_dir / "a.yaml", alpha_id="alpha_a")

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        monitor.apply_decision = MagicMock()  # type: ignore[assignment]
        scheduler = CanaryAutoScheduler(monitor=monitor, dry_run=True)

        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(scheduler.evaluate_all())
        finally:
            loop.close()

        assert len(results) == 1
        assert results[0].alpha_id == "alpha_a"
        assert results[0].state == "canary"
        monitor.apply_decision.assert_not_called()

    def test_apply_mode_calls_apply_decision(self, tmp_path: Path) -> None:
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(promo_dir / "a.yaml", alpha_id="alpha_a")

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        monitor.apply_decision = MagicMock()  # type: ignore[assignment]
        scheduler = CanaryAutoScheduler(monitor=monitor, dry_run=False)

        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(scheduler.evaluate_all())
        finally:
            loop.close()

        assert len(results) == 1
        monitor.apply_decision.assert_called_once()
        applied_status = monitor.apply_decision.call_args[0][0]
        assert isinstance(applied_status, CanaryStatus)
        assert applied_status.alpha_id == "alpha_a"

    def test_multiple_canaries(self, tmp_path: Path) -> None:
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(promo_dir / "a.yaml", alpha_id="alpha_a")
        _write_canary_yaml(promo_dir / "b.yaml", alpha_id="alpha_b")

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        scheduler = CanaryAutoScheduler(monitor=monitor, dry_run=True)

        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(scheduler.evaluate_all())
        finally:
            loop.close()

        assert len(results) == 2
        alpha_ids = {r.alpha_id for r in results}
        assert alpha_ids == {"alpha_a", "alpha_b"}

    def test_empty_promotions(self, tmp_path: Path) -> None:
        monitor = CanaryMonitor(promotions_dir=str(tmp_path / "empty"))
        scheduler = CanaryAutoScheduler(monitor=monitor, dry_run=True)

        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(scheduler.evaluate_all())
        finally:
            loop.close()

        assert results == []

    def test_single_canary_error_does_not_stop_others(self, tmp_path: Path) -> None:
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(promo_dir / "a.yaml", alpha_id="alpha_a")
        _write_canary_yaml(promo_dir / "b.yaml", alpha_id="alpha_b")

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        original_evaluate = monitor.evaluate
        call_count = 0

        def flaky_evaluate(alpha_id: str, metrics: dict) -> CanaryStatus:
            nonlocal call_count
            call_count += 1
            if alpha_id == "alpha_a":
                raise RuntimeError("simulated failure")
            return original_evaluate(alpha_id, metrics)

        monitor.evaluate = flaky_evaluate  # type: ignore[assignment]
        scheduler = CanaryAutoScheduler(monitor=monitor, dry_run=True)

        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(scheduler.evaluate_all())
        finally:
            loop.close()

        # alpha_a failed, alpha_b should still be evaluated
        assert len(results) == 1
        assert results[0].alpha_id == "alpha_b"


class TestBuildMetrics:
    def test_defaults_when_no_live_metrics(self) -> None:
        canary: dict = {"alpha_id": "test"}
        metrics = CanaryAutoScheduler._build_metrics(canary)
        assert metrics["slippage_bps"] == 0.0
        assert metrics["sessions_live"] == 0
        assert "sharpe_live" not in metrics

    def test_extracts_stored_metrics(self) -> None:
        canary: dict = {
            "alpha_id": "test",
            "live_metrics": {
                "slippage_bps": 1.5,
                "sessions_live": 12,
                "sharpe_live": 2.1,
            },
        }
        metrics = CanaryAutoScheduler._build_metrics(canary)
        assert metrics["slippage_bps"] == 1.5
        assert metrics["sessions_live"] == 12
        assert metrics["sharpe_live"] == 2.1

    def test_handles_non_dict_live_metrics(self) -> None:
        canary: dict = {"alpha_id": "test", "live_metrics": "invalid"}
        metrics = CanaryAutoScheduler._build_metrics(canary)
        assert metrics["slippage_bps"] == 0.0
