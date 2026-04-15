"""Tests for ContractLifecycleManager."""
from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestContractLifecycleManager:
    @pytest.mark.asyncio
    async def test_detect_expiry_warns_3_days_before(self):
        from hft_platform.ops.contract_lifecycle import ContractLifecycleManager
        alert_callback = AsyncMock()
        mgr = ContractLifecycleManager(
            contracts_runtime=MagicMock(), alert_callback=alert_callback, expiry_warn_days=[3, 1],
        )
        today = date(2026, 4, 16)
        mgr._known_expiries = {"TXFE6": today + timedelta(days=3)}
        await mgr.check_expiries(today)
        alert_callback.assert_awaited_once()
        alert = alert_callback.call_args.args[0]
        assert "TXFE6" in alert.title
        from hft_platform.notifications.alert import AlertSeverity
        assert alert.severity == AlertSeverity.INFO

    @pytest.mark.asyncio
    async def test_detect_expiry_warns_1_day_before(self):
        from hft_platform.ops.contract_lifecycle import ContractLifecycleManager
        alert_callback = AsyncMock()
        mgr = ContractLifecycleManager(
            contracts_runtime=MagicMock(), alert_callback=alert_callback, expiry_warn_days=[3, 1],
        )
        today = date(2026, 4, 16)
        mgr._known_expiries = {"TXFE6": today + timedelta(days=1)}
        await mgr.check_expiries(today)
        alert_callback.assert_awaited_once()
        from hft_platform.notifications.alert import AlertSeverity
        assert alert_callback.call_args.args[0].severity == AlertSeverity.WARN

    @pytest.mark.asyncio
    async def test_no_warning_for_distant_expiry(self):
        from hft_platform.ops.contract_lifecycle import ContractLifecycleManager
        alert_callback = AsyncMock()
        mgr = ContractLifecycleManager(
            contracts_runtime=MagicMock(), alert_callback=alert_callback, expiry_warn_days=[3, 1],
        )
        today = date(2026, 4, 16)
        mgr._known_expiries = {"TXFE6": today + timedelta(days=20)}
        await mgr.check_expiries(today)
        alert_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refresh_futures_aliases_calls_runtime(self):
        from hft_platform.ops.contract_lifecycle import ContractLifecycleManager
        contracts_runtime = MagicMock()
        contracts_runtime.refresh_contract_cache = AsyncMock()
        contracts_runtime.resolve_symbol_aliases = MagicMock(return_value={"TMFR1": "TMFE6"})
        mgr = ContractLifecycleManager(contracts_runtime=contracts_runtime, alert_callback=AsyncMock())
        result = await mgr.refresh_futures_aliases()
        contracts_runtime.refresh_contract_cache.assert_awaited_once()
        assert result == {"TMFR1": "TMFE6"}

    @pytest.mark.asyncio
    async def test_refresh_option_chain(self):
        from hft_platform.ops.contract_lifecycle import ContractLifecycleManager
        contracts_runtime = MagicMock()
        mock_contract = MagicMock()
        mock_contract.code = "TXO22500C6"
        contracts_runtime.get_option_contracts = AsyncMock(return_value=[mock_contract])
        mgr = ContractLifecycleManager(
            contracts_runtime=contracts_runtime, alert_callback=AsyncMock(), option_strike_range=10,
        )
        contracts = await mgr.refresh_option_chain(underlying_price=22500)
        assert isinstance(contracts, list)
        contracts_runtime.get_option_contracts.assert_awaited_once()
