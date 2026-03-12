"""Unit tests for FubonContractsRuntime."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.fubon.contracts_runtime import FubonContractsRuntime


@pytest.fixture()
def mock_sdk() -> MagicMock:
    return MagicMock()


# ------------------------------------------------------------------ #
# Init tests
# ------------------------------------------------------------------ #


class TestInit:
    def test_init_from_list_config(self, mock_sdk: MagicMock) -> None:
        symbols = [
            {"code": "2330", "exchange": "TSE"},
            {"code": "2317", "exchange": "TSE"},
        ]
        rt = FubonContractsRuntime(mock_sdk, symbols_config=symbols)
        assert len(rt.symbols) == 2
        assert rt.get_exchange("2330") == "TSE"

    def test_init_from_dict_config(self, mock_sdk: MagicMock) -> None:
        config: dict[str, Any] = {
            "symbols": [
                {"code": "2330", "exchange": "TSE"},
                {"code": "6547", "exchange": "OTC"},
            ]
        }
        rt = FubonContractsRuntime(mock_sdk, symbols_config=config)
        assert len(rt.symbols) == 2
        assert rt.get_exchange("6547") == "OTC"

    def test_init_no_config(self, mock_sdk: MagicMock) -> None:
        rt = FubonContractsRuntime(mock_sdk)
        assert rt.symbols == []
        assert rt.get_exchange("2330") == ""

    def test_init_from_yaml_file(self, mock_sdk: MagicMock) -> None:
        yaml_content = "symbols:\n  - code: '2330'\n    exchange: TSE\n  - code: TXFG6\n    exchange: TAIFEX\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            rt = FubonContractsRuntime(mock_sdk, symbols_path=f.name)
        assert len(rt.symbols) == 2
        assert rt.get_exchange("2330") == "TSE"
        assert rt.get_exchange("TXFG6") == "TAIFEX"

    def test_init_config_takes_priority_over_path(self, mock_sdk: MagicMock) -> None:
        """When both symbols_config and symbols_path are given, config wins."""
        rt = FubonContractsRuntime(
            mock_sdk,
            symbols_config=[{"code": "AAA", "exchange": "TSE"}],
            symbols_path="/nonexistent/path.yaml",
        )
        assert len(rt.symbols) == 1
        assert rt.get_exchange("AAA") == "TSE"


# ------------------------------------------------------------------ #
# validate_symbols tests
# ------------------------------------------------------------------ #


class TestValidateSymbols:
    def test_valid_symbols_returns_empty(self, mock_sdk: MagicMock) -> None:
        symbols = [
            {"code": "2330", "exchange": "TSE"},
            {"code": "TXFG6", "exchange": "TAIFEX"},
            {"code": "AbCd12345678901234", "exchange": "TSE"},  # 18 chars, valid
        ]
        rt = FubonContractsRuntime(mock_sdk, symbols_config=symbols)
        assert rt.validate_symbols() == []

    def test_invalid_symbols_detected(self, mock_sdk: MagicMock) -> None:
        symbols = [
            {"code": "2330", "exchange": "TSE"},
            {"code": "BAD SPACE", "exchange": "TSE"},
            {"code": "", "exchange": "TSE"},
            {"code": "A" * 21, "exchange": "TSE"},  # 21 chars, too long
            {"code": "OK", "exchange": "TSE"},
        ]
        rt = FubonContractsRuntime(mock_sdk, symbols_config=symbols)
        invalid = rt.validate_symbols()
        assert "BAD SPACE" in invalid
        assert "" in invalid
        assert "A" * 21 in invalid
        assert "2330" not in invalid
        assert "OK" not in invalid

    def test_special_chars_invalid(self, mock_sdk: MagicMock) -> None:
        symbols = [{"code": "A-B", "exchange": "TSE"}, {"code": "A.B", "exchange": "TSE"}]
        rt = FubonContractsRuntime(mock_sdk, symbols_config=symbols)
        invalid = rt.validate_symbols()
        assert len(invalid) == 2


# ------------------------------------------------------------------ #
# get_exchange tests
# ------------------------------------------------------------------ #


class TestGetExchange:
    def test_known_symbol(self, mock_sdk: MagicMock) -> None:
        rt = FubonContractsRuntime(
            mock_sdk,
            symbols_config=[
                {"code": "2330", "exchange": "TSE"},
                {"code": "6547", "exchange": "OTC"},
            ],
        )
        assert rt.get_exchange("2330") == "TSE"
        assert rt.get_exchange("6547") == "OTC"

    def test_unknown_symbol_returns_empty(self, mock_sdk: MagicMock) -> None:
        rt = FubonContractsRuntime(
            mock_sdk,
            symbols_config=[{"code": "2330", "exchange": "TSE"}],
        )
        assert rt.get_exchange("9999") == ""

    def test_empty_config_returns_empty(self, mock_sdk: MagicMock) -> None:
        rt = FubonContractsRuntime(mock_sdk)
        assert rt.get_exchange("2330") == ""


# ------------------------------------------------------------------ #
# reload_symbols tests
# ------------------------------------------------------------------ #


class TestReloadSymbols:
    def test_reload_detects_added_removed(self, mock_sdk: MagicMock) -> None:
        yaml_v1 = "symbols:\n  - code: '2330'\n    exchange: TSE\n  - code: '2317'\n    exchange: TSE\n"
        yaml_v2 = "symbols:\n  - code: '2330'\n    exchange: TSE\n  - code: '6547'\n    exchange: OTC\n"

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_v1)
            f.flush()
            path = f.name

        rt = FubonContractsRuntime(mock_sdk, symbols_path=path)
        assert rt.get_exchange("2317") == "TSE"
        assert rt.get_exchange("6547") == ""

        # Overwrite with v2
        Path(path).write_text(yaml_v2, encoding="utf-8")
        rt.reload_symbols()

        assert rt.get_exchange("2317") == ""  # removed
        assert rt.get_exchange("6547") == "OTC"  # added
        assert rt.get_exchange("2330") == "TSE"  # still there

    def test_reload_no_path_uses_existing_config(self, mock_sdk: MagicMock) -> None:
        symbols = [{"code": "2330", "exchange": "TSE"}]
        rt = FubonContractsRuntime(mock_sdk, symbols_config=symbols)

        # reload without path — should keep existing config
        rt.reload_symbols()
        assert rt.get_exchange("2330") == "TSE"

    def test_reload_updates_last_reload_ts(self, mock_sdk: MagicMock) -> None:
        rt = FubonContractsRuntime(mock_sdk, symbols_config=[])
        ts_before = rt.refresh_status()["last_reload_ts"]
        rt.reload_symbols()
        ts_after = rt.refresh_status()["last_reload_ts"]
        assert ts_after >= ts_before


# ------------------------------------------------------------------ #
# refresh_status tests
# ------------------------------------------------------------------ #


class TestRefreshStatus:
    def test_returns_expected_keys(self, mock_sdk: MagicMock) -> None:
        rt = FubonContractsRuntime(
            mock_sdk,
            symbols_config=[
                {"code": "2330", "exchange": "TSE"},
                {"code": "2317", "exchange": "TSE"},
            ],
        )
        status = rt.refresh_status()
        assert status["symbol_count"] == 2
        assert status["exchange_map_size"] == 2
        assert isinstance(status["last_reload_ts"], int)
        assert status["last_reload_ts"] > 0

    def test_empty_config_status(self, mock_sdk: MagicMock) -> None:
        rt = FubonContractsRuntime(mock_sdk)
        status = rt.refresh_status()
        assert status["symbol_count"] == 0
        assert status["exchange_map_size"] == 0


# ------------------------------------------------------------------ #
# symbols property tests
# ------------------------------------------------------------------ #


class TestSymbolsProperty:
    def test_returns_copy(self, mock_sdk: MagicMock) -> None:
        symbols = [{"code": "2330", "exchange": "TSE"}]
        rt = FubonContractsRuntime(mock_sdk, symbols_config=symbols)
        result = rt.symbols
        result.append({"code": "EXTRA", "exchange": "OTC"})
        # Original should be unaffected
        assert len(rt.symbols) == 1

    def test_symbols_from_dict_config_with_symbols_key(self, mock_sdk: MagicMock) -> None:
        config: dict[str, Any] = {
            "symbols": [
                {"code": "A", "exchange": "TSE"},
                {"code": "B", "exchange": "OTC"},
                {"code": "C", "exchange": "ESB"},
            ]
        }
        rt = FubonContractsRuntime(mock_sdk, symbols_config=config)
        assert len(rt.symbols) == 3


# ------------------------------------------------------------------ #
# Edge cases
# ------------------------------------------------------------------ #


class TestEdgeCases:
    def test_missing_code_or_exchange_skipped_in_map(self, mock_sdk: MagicMock) -> None:
        symbols = [
            {"code": "2330", "exchange": "TSE"},
            {"code": "", "exchange": "TSE"},  # empty code
            {"code": "2317"},  # missing exchange
            {"exchange": "OTC"},  # missing code
        ]
        rt = FubonContractsRuntime(mock_sdk, symbols_config=symbols)
        assert rt.get_exchange("2330") == "TSE"
        assert rt.refresh_status()["exchange_map_size"] == 1

    def test_dict_config_without_symbols_key(self, mock_sdk: MagicMock) -> None:
        config: dict[str, Any] = {"other_key": "value"}
        rt = FubonContractsRuntime(mock_sdk, symbols_config=config)
        assert rt.symbols == []

    def test_yaml_load_nonexistent_file(self, mock_sdk: MagicMock) -> None:
        rt = FubonContractsRuntime(mock_sdk, symbols_path="/nonexistent/symbols.yaml")
        assert rt.symbols == []
        assert rt.get_exchange("2330") == ""
