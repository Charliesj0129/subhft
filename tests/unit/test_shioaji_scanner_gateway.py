"""Tests for ScannerGateway — Shioaji market scanner API wrapper."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Stub shioaji before importing the module under test
# ---------------------------------------------------------------------------

def _build_sj_stub() -> types.ModuleType:
    """Create a minimal shioaji stub with ScannerType enum."""
    sj = types.ModuleType("shioaji")
    constant = types.ModuleType("shioaji.constant")

    class _ScannerType:
        ChangePercentRank = "ChangePercentRank"
        ChangePriceRank = "ChangePriceRank"
        DayRangeRank = "DayRangeRank"
        VolumeRank = "VolumeRank"
        AmountRank = "AmountRank"

    constant.ScannerType = _ScannerType  # type: ignore[attr-defined]
    sj.constant = constant  # type: ignore[attr-defined]
    return sj


_sj_stub = _build_sj_stub()
sys.modules.setdefault("shioaji", _sj_stub)
sys.modules.setdefault("shioaji.constant", _sj_stub.constant)  # type: ignore[arg-type]

from hft_platform.feed_adapter.shioaji.scanner_gateway import (  # noqa: E402
    ScannerGateway,
    _VALID_SCANNER_TYPES,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_client() -> MagicMock:
    client = MagicMock()
    client.mode = "live"
    client._cache_get.return_value = None
    client._rate_limit_api.return_value = True
    client.api.scanners.return_value = [{"code": "2330", "rank": 1}]
    return client


@pytest.fixture()
def gateway(mock_client: MagicMock) -> ScannerGateway:
    return ScannerGateway(mock_client)


# ---------------------------------------------------------------------------
# scan() tests
# ---------------------------------------------------------------------------

class TestScan:
    def test_valid_scanner_type(self, gateway: ScannerGateway, mock_client: MagicMock) -> None:
        result = gateway.scan("VolumeRank", count=10)
        assert result == [{"code": "2330", "rank": 1}]
        mock_client.api.scanners.assert_called_once()
        call_kwargs = mock_client.api.scanners.call_args[1]
        assert call_kwargs["count"] == 10
        assert call_kwargs["ascending"] is False

    def test_invalid_scanner_type_raises(self, gateway: ScannerGateway) -> None:
        with pytest.raises(ValueError, match="Invalid scanner_type"):
            gateway.scan("InvalidType")

    def test_scanner_type_enum_mapping(self, gateway: ScannerGateway, mock_client: MagicMock) -> None:
        """Each valid type string is mapped to the SDK enum attribute."""
        for st in _VALID_SCANNER_TYPES:
            mock_client._cache_get.return_value = None
            gateway.scan(st)
            call_kwargs = mock_client.api.scanners.call_args[1]
            assert call_kwargs["scanner_type"] == getattr(_sj_stub.constant.ScannerType, st)

    def test_cache_hit_returns_cached(self, gateway: ScannerGateway, mock_client: MagicMock) -> None:
        cached_data = [{"code": "2317", "rank": 2}]
        mock_client._cache_get.return_value = cached_data
        result = gateway.scan("VolumeRank")
        assert result is cached_data
        mock_client.api.scanners.assert_not_called()

    def test_rate_limit_rejection(self, gateway: ScannerGateway, mock_client: MagicMock) -> None:
        mock_client._rate_limit_api.return_value = False
        result = gateway.scan("VolumeRank")
        assert result == []
        mock_client.api.scanners.assert_not_called()

    def test_simulation_mode_returns_empty(self, mock_client: MagicMock) -> None:
        mock_client.mode = "simulation"
        gw = ScannerGateway(mock_client)
        result = gw.scan("VolumeRank")
        assert result == []
        mock_client.api.scanners.assert_not_called()

    def test_api_error_returns_empty(self, gateway: ScannerGateway, mock_client: MagicMock) -> None:
        mock_client.api.scanners.side_effect = RuntimeError("connection lost")
        result = gateway.scan("VolumeRank")
        assert result == []
        mock_client._record_api_latency.assert_called_once()
        mock_client._record_api_latency.assert_called_with("scanners", mock_client._record_api_latency.call_args[0][1], ok=False)

    def test_latency_recorded_on_success(self, gateway: ScannerGateway, mock_client: MagicMock) -> None:
        gateway.scan("AmountRank")
        mock_client._record_api_latency.assert_called_once()
        mock_client._record_api_latency.assert_called_with("scanners", mock_client._record_api_latency.call_args[0][1], ok=True)

    def test_cache_set_on_success(self, gateway: ScannerGateway, mock_client: MagicMock) -> None:
        gateway.scan("VolumeRank", ascending=True, count=20)
        mock_client._cache_set.assert_called_once()
        key = mock_client._cache_set.call_args[0][0]
        assert "scanner:VolumeRank:True:20" == key


# ---------------------------------------------------------------------------
# scan_multiple() tests
# ---------------------------------------------------------------------------

class TestScanMultiple:
    def test_calls_scan_for_each_type(self, gateway: ScannerGateway, mock_client: MagicMock) -> None:
        results = gateway.scan_multiple(scanner_types=["VolumeRank", "AmountRank"], count=5)
        assert "VolumeRank" in results
        assert "AmountRank" in results
        assert len(results) == 2

    def test_defaults_to_all_types(self, gateway: ScannerGateway, mock_client: MagicMock) -> None:
        results = gateway.scan_multiple()
        assert set(results.keys()) == _VALID_SCANNER_TYPES

    def test_handles_per_type_errors(self, gateway: ScannerGateway, mock_client: MagicMock) -> None:
        """If one type raises ValueError (invalid), it is caught and others continue."""
        results = gateway.scan_multiple(scanner_types=["VolumeRank", "BadType", "AmountRank"])
        # BadType should produce empty list (ValueError caught)
        assert results["BadType"] == []
        # Valid types still have results
        assert results["VolumeRank"] == [{"code": "2330", "rank": 1}]
        assert results["AmountRank"] == [{"code": "2330", "rank": 1}]

    def test_api_error_in_one_type(self, gateway: ScannerGateway, mock_client: MagicMock) -> None:
        """API error for one scanner type does not block others."""
        call_count = 0
        original_return = [{"code": "2330", "rank": 1}]

        def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("timeout")
            return original_return

        mock_client.api.scanners.side_effect = side_effect
        results = gateway.scan_multiple(scanner_types=["VolumeRank", "AmountRank", "DayRangeRank"])
        # At least 2 of 3 should have results (one fails with empty list)
        non_empty = [v for v in results.values() if v]
        assert len(non_empty) >= 2
