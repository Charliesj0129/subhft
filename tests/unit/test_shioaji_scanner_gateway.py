from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hft_platform.feed_adapter.shioaji.scanner_gateway import (
    ScannerGateway,
    _VALID_SCANNER_TYPES,
)


def _make_client(
    mode: str = "live",
    logged_in: bool = True,
    has_api: bool = True,
    has_scanners: bool = True,
) -> MagicMock:
    client = MagicMock()
    client.mode = mode
    client.logged_in = logged_in
    if has_api:
        api = MagicMock()
        if not has_scanners:
            del api.scanners
        client.api = api
    else:
        client.api = None
    client._rate_limit_api = MagicMock(return_value=True)
    client._record_api_latency = MagicMock()
    return client


def _make_sdk_with_scanner_type() -> MagicMock:
    sdk = MagicMock()
    scanner_type_cls = SimpleNamespace(
        ChangePercentRank="CPR",
        ChangePriceRank="CPRC",
        DayRangeRank="DRR",
        VolumeRank="VR",
        AmountRank="AR",
    )
    sdk.constant.ScannerType = scanner_type_cls
    return sdk


class TestScannerGateway:
    def test_scan_returns_results(self) -> None:
        client = _make_client()
        client.api.scanners.return_value = [{"code": "2330", "change_percent": 5.0}]
        gw = ScannerGateway(client)

        with patch.object(ScannerGateway, "_sdk", return_value=_make_sdk_with_scanner_type()):
            result = gw.scan("VolumeRank", count=10)

        assert len(result) == 1
        assert result[0]["code"] == "2330"
        client.api.scanners.assert_called_once_with(
            scanner_type="VR",
            ascending=False,
            count=10,
            date=None,
            timeout=30000,
        )
        client._rate_limit_api.assert_called_once_with("scanners")
        client._record_api_latency.assert_called_once()
        call_args = client._record_api_latency.call_args
        assert call_args[0][0] == "scanners"
        assert call_args[1]["ok"] is True

    def test_scan_ascending(self) -> None:
        client = _make_client()
        client.api.scanners.return_value = []
        gw = ScannerGateway(client)

        with patch.object(ScannerGateway, "_sdk", return_value=_make_sdk_with_scanner_type()):
            result = gw.scan("AmountRank", ascending=True, count=50)

        assert result == []
        client.api.scanners.assert_called_once_with(
            scanner_type="AR",
            ascending=True,
            count=50,
            date=None,
            timeout=30000,
        )

    def test_scan_with_date(self) -> None:
        client = _make_client()
        client.api.scanners.return_value = [{"code": "2317"}]
        gw = ScannerGateway(client)

        with patch.object(ScannerGateway, "_sdk", return_value=_make_sdk_with_scanner_type()):
            result = gw.scan("ChangePercentRank", date="2026-03-12")

        assert len(result) == 1
        client.api.scanners.assert_called_once_with(
            scanner_type="CPR",
            ascending=False,
            count=100,
            date="2026-03-12",
            timeout=30000,
        )

    def test_scan_invalid_scanner_type_returns_empty(self) -> None:
        client = _make_client()
        gw = ScannerGateway(client)

        result = gw.scan("InvalidType")

        assert result == []
        client.api.scanners.assert_not_called()

    def test_scan_simulation_mode_returns_empty(self) -> None:
        client = _make_client(mode="simulation")
        gw = ScannerGateway(client)

        result = gw.scan("VolumeRank")

        assert result == []
        client.api.scanners.assert_not_called()

    def test_scan_no_api_returns_empty(self) -> None:
        client = _make_client(has_api=False)
        gw = ScannerGateway(client)

        result = gw.scan("VolumeRank")

        assert result == []

    def test_scan_not_logged_in_returns_empty(self) -> None:
        client = _make_client(logged_in=False)
        gw = ScannerGateway(client)

        result = gw.scan("VolumeRank")

        assert result == []
        client.api.scanners.assert_not_called()

    def test_scan_api_missing_scanners_method_returns_empty(self) -> None:
        client = _make_client(has_scanners=False)
        gw = ScannerGateway(client)

        result = gw.scan("VolumeRank")

        assert result == []

    def test_scan_sdk_unavailable_returns_empty(self) -> None:
        client = _make_client()
        gw = ScannerGateway(client)

        with patch.object(ScannerGateway, "_sdk", return_value=None):
            result = gw.scan("VolumeRank")

        assert result == []
        client.api.scanners.assert_not_called()

    def test_scan_scanner_type_constant_missing_returns_empty(self) -> None:
        client = _make_client()
        gw = ScannerGateway(client)

        sdk = MagicMock()
        sdk.constant.ScannerType = SimpleNamespace()  # no attributes
        with patch.object(ScannerGateway, "_sdk", return_value=sdk):
            result = gw.scan("VolumeRank")

        assert result == []
        client.api.scanners.assert_not_called()

    def test_scan_rate_limited_returns_empty(self) -> None:
        client = _make_client()
        client._rate_limit_api.return_value = False
        gw = ScannerGateway(client)

        with patch.object(ScannerGateway, "_sdk", return_value=_make_sdk_with_scanner_type()):
            result = gw.scan("VolumeRank")

        assert result == []
        client.api.scanners.assert_not_called()

    def test_scan_api_exception_returns_empty(self) -> None:
        client = _make_client()
        client.api.scanners.side_effect = RuntimeError("network error")
        gw = ScannerGateway(client)

        with patch.object(ScannerGateway, "_sdk", return_value=_make_sdk_with_scanner_type()):
            result = gw.scan("VolumeRank")

        assert result == []
        client._record_api_latency.assert_called_once()
        call_args = client._record_api_latency.call_args
        assert call_args[1]["ok"] is False

    def test_scan_api_returns_none_returns_empty(self) -> None:
        client = _make_client()
        client.api.scanners.return_value = None
        gw = ScannerGateway(client)

        with patch.object(ScannerGateway, "_sdk", return_value=_make_sdk_with_scanner_type()):
            result = gw.scan("VolumeRank")

        assert result == []

    def test_all_valid_scanner_types_resolve(self) -> None:
        sdk = _make_sdk_with_scanner_type()
        for st in _VALID_SCANNER_TYPES:
            resolved = ScannerGateway._resolve_scanner_type(sdk, st)
            assert resolved is not None, f"Failed to resolve {st}"

    def test_resolve_scanner_type_missing_constant_returns_none(self) -> None:
        sdk = MagicMock()
        sdk.constant = SimpleNamespace()  # no ScannerType
        assert ScannerGateway._resolve_scanner_type(sdk, "VolumeRank") is None

    def test_slots(self) -> None:
        assert "__slots__" in ScannerGateway.__dict__
        assert "_client" in ScannerGateway.__slots__


class TestFacadeScannerWiring:
    def test_facade_has_scanner_gateway(self, tmp_path) -> None:
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n")

        with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
            mock_api = MagicMock()
            mock_sj.Shioaji.return_value = mock_api

            from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

            facade = ShioajiClientFacade(str(cfg), {})

            assert facade.scanner_gateway is not None
            assert isinstance(facade.scanner_gateway, ScannerGateway)

    def test_facade_scan_delegates_to_gateway(self, tmp_path) -> None:
        cfg = tmp_path / "symbols.yaml"
        cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n")

        with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
            mock_api = MagicMock()
            mock_sj.Shioaji.return_value = mock_api

            from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

            facade = ShioajiClientFacade(str(cfg), {})

            with patch.object(
                type(facade.scanner_gateway),
                "scan",
                return_value=[{"code": "2330"}],
            ) as mock_scan:
                result = facade.scan("VolumeRank", ascending=True, count=50)

            assert result == [{"code": "2330"}]
            mock_scan.assert_called_once_with(
                scanner_type="VolumeRank",
                ascending=True,
                count=50,
                date=None,
                timeout=30000,
            )
