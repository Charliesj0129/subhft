"""Tests for hft_platform.alpha.canary_scheduler module."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        assert scheduler._task is None

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
        assert scheduler._task is None

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
        assert scheduler._task is None


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
        """When live_metrics is missing, use fail-safe (worst-case) defaults."""
        canary: dict = {"alpha_id": "test"}
        metrics = CanaryAutoScheduler._build_metrics(canary)
        # Fail-safe defaults: exceed rollback thresholds
        assert metrics["slippage_bps"] == 999.0
        assert metrics["drawdown_contribution"] == 1.0
        assert metrics["execution_error_rate"] == 1.0
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
        """When live_metrics is not a dict, use fail-safe defaults."""
        canary: dict = {"alpha_id": "test", "live_metrics": "invalid"}
        metrics = CanaryAutoScheduler._build_metrics(canary)
        # Fail-safe defaults
        assert metrics["slippage_bps"] == 999.0
        assert metrics["drawdown_contribution"] == 1.0
        assert metrics["execution_error_rate"] == 1.0


class TestSchedulerMetricsQuery:
    def test_evaluate_all_no_query_backward_compat(self, tmp_path: Path) -> None:
        """metrics_query=None → existing YAML-based _build_metrics behavior."""
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(
            promo_dir / "a.yaml",
            alpha_id="alpha_a",
            slippage_bps=1.0,
            sessions_live=5,
        )

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        scheduler = CanaryAutoScheduler(monitor=monitor, dry_run=True, metrics_query=None)

        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(scheduler.evaluate_all())
        finally:
            loop.close()

        assert len(results) == 1
        assert results[0].alpha_id == "alpha_a"
        # slippage=1.0 is below the 3.0 guardrail → canary continues
        assert results[0].state == "canary"

    def test_evaluate_all_uses_ck_metrics(self, tmp_path: Path) -> None:
        """Mock query returns good metrics → canary NOT rolled back."""
        promo_dir = tmp_path / "promos"
        # Write YAML with empty live_metrics (would trigger fail-safe rollback if used)
        promo_dir.mkdir(parents=True, exist_ok=True)
        import yaml as _yaml

        payload = {
            "alpha_id": "alpha_ck",
            "enabled": True,
            "weight": 0.02,
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
            "scorecard_snapshot": {"sharpe_oos": 1.5},
            # intentionally no live_metrics block
        }
        (promo_dir / "ck.yaml").write_text(_yaml.safe_dump(payload, sort_keys=False))

        mock_query = MagicMock()
        mock_query.fetch.return_value = {
            "slippage_bps": 0.5,
            "drawdown_contribution": 0.001,
            "execution_error_rate": 0.0,
            "sessions_live": 10,
        }

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        scheduler = CanaryAutoScheduler(monitor=monitor, dry_run=True, metrics_query=mock_query)

        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(scheduler.evaluate_all())
        finally:
            loop.close()

        assert len(results) == 1
        assert results[0].alpha_id == "alpha_ck"
        # Good CK metrics → should stay in canary (not rolled back)
        assert results[0].state == "canary"
        mock_query.fetch.assert_called_once_with("alpha_ck", "alpha_ck", 0)

    def test_evaluate_all_fallback_on_none(self, tmp_path: Path) -> None:
        """Mock query returns None → falls back to _build_metrics (fail-safe defaults trigger rollback)."""
        promo_dir = tmp_path / "promos"
        # Write YAML with no live_metrics → _build_metrics will return fail-safe defaults
        promo_dir.mkdir(parents=True, exist_ok=True)
        import yaml as _yaml

        payload = {
            "alpha_id": "alpha_fb",
            "enabled": True,
            "weight": 0.02,
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
            "scorecard_snapshot": {"sharpe_oos": 1.5},
            # no live_metrics → fail-safe defaults → rollback
        }
        (promo_dir / "fb.yaml").write_text(_yaml.safe_dump(payload, sort_keys=False))

        mock_query = MagicMock()
        mock_query.fetch.return_value = None  # query returns None → fall back

        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        scheduler = CanaryAutoScheduler(monitor=monitor, dry_run=True, metrics_query=mock_query)

        loop = asyncio.new_event_loop()
        try:
            results = loop.run_until_complete(scheduler.evaluate_all())
        finally:
            loop.close()

        assert len(results) == 1
        assert results[0].alpha_id == "alpha_fb"
        # Fail-safe defaults from _build_metrics should trigger rollback
        assert results[0].state == "rolled_back"
        mock_query.fetch.assert_called_once()
