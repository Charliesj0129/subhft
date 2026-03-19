"""WU-04: Reconciliation Resilience tests."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.execution.reconciliation import (
    ReconciliationService,
    _compute_backoff_delay,
)


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


class TestReconciliationResilience:
    def test_default_grace_failures(self) -> None:
        svc = _make_service()
        assert svc.grace_failures == 10

    def test_default_check_interval(self) -> None:
        svc = _make_service()
        assert svc.check_interval_s == 5.0

    def test_config_override_grace_failures(self) -> None:
        cfg = {"reconciliation": {"grace_failures": 20}}
        svc = _make_service(config=cfg)
        assert svc.grace_failures == 20

    def test_backoff_exponential_growth(self) -> None:
        with patch("hft_platform.execution.reconciliation.random") as mock_rng:
            mock_rng.uniform.return_value = 1.0
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

    @pytest.mark.asyncio
    async def test_halt_triggered_at_grace_limit(self) -> None:
        sg = MagicMock()

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
                return
            raise RuntimeError("down")

        svc.sync_portfolio = _always_fail  # type: ignore[assignment]

        with patch.object(svc, "_update_failure_gauge"):
            await asyncio.wait_for(svc.run(), timeout=5.0)

        sg.trigger_halt.assert_called_once()
        assert "RECONCILIATION_UNAVAILABLE" in sg.trigger_halt.call_args[0][0]
