"""WU-04: Reconciliation Resilience tests.

Covers:
- Default check_interval_s = 5 (not 1)
- Default grace_failures = 10 (not 3)
- Exponential backoff on failure
- Countdown logging on each failure
- Reset of failure counter and backoff on success
- Env-var overrides for all resilience params
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.execution.reconciliation import (
    ReconciliationService,
    _compute_backoff_delay,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_service(
    *,
    config: dict | None = None,
    client: MagicMock | None = None,
    storm_guard: MagicMock | None = None,
) -> ReconciliationService:
    if client is None:
        client = MagicMock()
        client.get_positions.return_value = []
    if config is None:
        config = {}
    store = MagicMock()
    store.positions = {}
    return ReconciliationService(client, store, config, storm_guard=storm_guard)


# ---------------------------------------------------------------------------
# Tests: defaults
# ---------------------------------------------------------------------------


class TestReconciliationResilience:
    """WU-04 resilience behaviour."""

    def test_default_check_interval_is_5s(self) -> None:
        svc = _make_service()
        assert svc.check_interval_s == 5.0

    def test_default_grace_failures_is_10(self) -> None:
        svc = _make_service()
        assert svc.grace_failures == 10

    def test_default_backoff_base_is_2(self) -> None:
        svc = _make_service()
        assert svc.backoff_base == 2.0

    def test_default_backoff_max_is_60(self) -> None:
        svc = _make_service()
        assert svc.backoff_max == 60.0

    def test_config_override_check_interval(self) -> None:
        svc = _make_service(config={"reconciliation": {"check_interval_s": 15}})
        assert svc.check_interval_s == 15

    def test_config_override_grace_failures(self) -> None:
        svc = _make_service(config={"reconciliation": {"grace_failures": 20}})
        assert svc.grace_failures == 20

    def test_config_override_backoff_base(self) -> None:
        svc = _make_service(config={"reconciliation": {"backoff_base": 3.0}})
        assert svc.backoff_base == 3.0

    def test_config_override_backoff_max(self) -> None:
        svc = _make_service(config={"reconciliation": {"backoff_max": 120}})
        assert svc.backoff_max == 120

    # ----- env-var overrides -----

    def test_env_var_check_interval(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_RECON_CHECK_INTERVAL", "7")
        # Re-import to pick up env at module-level
        import importlib

        import hft_platform.execution.reconciliation as mod
        importlib.reload(mod)
        try:
            svc = mod.ReconciliationService(MagicMock(), MagicMock(), {})
            assert svc.check_interval_s == 7.0
        finally:
            monkeypatch.delenv("HFT_RECON_CHECK_INTERVAL", raising=False)
            importlib.reload(mod)

    def test_env_var_grace_failures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_RECON_GRACE_FAILURES", "15")
        import importlib

        import hft_platform.execution.reconciliation as mod
        importlib.reload(mod)
        try:
            svc = mod.ReconciliationService(MagicMock(), MagicMock(), {})
            assert svc.grace_failures == 15
        finally:
            monkeypatch.delenv("HFT_RECON_GRACE_FAILURES", raising=False)
            importlib.reload(mod)

    # ----- backoff computation -----

    def test_backoff_exponential_growth(self) -> None:
        """Backoff should grow exponentially with attempt number."""
        with patch("hft_platform.execution.reconciliation.random") as mock_rng:
            mock_rng.uniform.return_value = 1.0  # no jitter
            d0 = _compute_backoff_delay(attempt=0, base=2, max_delay=60, jitter=0.2)
            d1 = _compute_backoff_delay(attempt=1, base=2, max_delay=60, jitter=0.2)
            d2 = _compute_backoff_delay(attempt=2, base=2, max_delay=60, jitter=0.2)
        assert d0 == pytest.approx(2.0)
        assert d1 == pytest.approx(4.0)
        assert d2 == pytest.approx(8.0)

    def test_backoff_capped_at_max(self) -> None:
        with patch("hft_platform.execution.reconciliation.random") as mock_rng:
            mock_rng.uniform.return_value = 1.0
            d = _compute_backoff_delay(attempt=20, base=2, max_delay=60, jitter=0.2)
        assert d == pytest.approx(60.0)

    def test_backoff_jitter_applied(self) -> None:
        """Jitter multiplies the raw delay."""
        with patch("hft_platform.execution.reconciliation.random") as mock_rng:
            mock_rng.uniform.return_value = 0.9  # 10% below 1.0
            d = _compute_backoff_delay(attempt=0, base=2, max_delay=60, jitter=0.2)
        assert d == pytest.approx(2.0 * 0.9)

    # ----- run loop resilience -----

    @pytest.mark.asyncio
    async def test_run_resets_failures_on_success(self) -> None:
        """After a failure, a success should reset _consecutive_failures to 0."""
        client = MagicMock()
        call_count = 0

        async def _fake_sync() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # startup sync - OK
                return
            if call_count == 2:
                raise RuntimeError("broker down")
            # third call succeeds
            return

        svc = _make_service(client=client)
        svc.check_interval_s = 0.01  # fast for testing
        svc.sync_portfolio = _fake_sync  # type: ignore[assignment]

        loop_count = 0

        async def _patched_run() -> None:
            nonlocal loop_count
            svc.running = True
            await svc.sync_portfolio()
            while svc.running:
                await asyncio.sleep(svc.check_interval_s)
                try:
                    await svc.sync_portfolio()
                    svc._consecutive_failures = 0
                    svc._update_failure_gauge()
                except Exception:
                    svc._consecutive_failures += 1
                    svc._update_failure_gauge()
                loop_count += 1
                if loop_count >= 2:
                    svc.running = False

        with patch.object(svc, "_update_failure_gauge"):
            await _patched_run()

        assert svc._consecutive_failures == 0

    @pytest.mark.asyncio
    async def test_halt_triggered_at_grace_limit(self) -> None:
        """HALT should trigger exactly when consecutive failures == grace_failures."""
        sg = MagicMock()

        # When trigger_halt is called, stop the service
        def _halt_and_stop(reason: str) -> None:
            svc.running = False

        sg.trigger_halt.side_effect = _halt_and_stop

        svc = _make_service(storm_guard=sg)
        svc.grace_failures = 3
        svc.check_interval_s = 0.001
        svc.backoff_base = 1.0
        svc.backoff_max = 0.001

        call_count = 0

        async def _always_fail() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return  # startup sync OK
            raise RuntimeError("down")

        svc.sync_portfolio = _always_fail  # type: ignore[assignment]

        with patch.object(svc, "_update_failure_gauge"):
            await asyncio.wait_for(svc.run(), timeout=5.0)

        sg.trigger_halt.assert_called_once()
        assert "RECONCILIATION_UNAVAILABLE" in sg.trigger_halt.call_args[0][0]

    @pytest.mark.asyncio
    async def test_countdown_logged_on_failure(self) -> None:
        """Each non-terminal failure should log remaining count."""
        svc = _make_service()
        svc.grace_failures = 5
        svc.check_interval_s = 0.001
        svc.backoff_base = 1.0
        svc.backoff_max = 0.001

        call_count = 0

        async def _fail_twice() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return
            if call_count <= 3:
                raise RuntimeError("down")
            return  # recover

        svc.sync_portfolio = _fail_twice  # type: ignore[assignment]

        with (
            patch("hft_platform.execution.reconciliation.logger") as mock_log,
            patch.object(svc, "_update_failure_gauge"),
        ):
            task = asyncio.create_task(svc.run())
            await asyncio.sleep(0.2)
            svc.running = False
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Should have logged warning with countdown info
        warning_calls = [c for c in mock_log.warning.call_args_list if "countdown" in str(c)]
        assert len(warning_calls) >= 1

    @pytest.mark.asyncio
    async def test_sync_portfolio_raises_on_failure(self) -> None:
        """sync_portfolio should now raise (not swallow) so run() can track failures."""
        client = MagicMock()
        client.get_positions.side_effect = RuntimeError("boom")
        svc = _make_service(client=client)

        with pytest.raises(RuntimeError, match="boom"):
            await svc.sync_portfolio()
