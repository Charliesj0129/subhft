"""Unit tests for FubonContractsRuntime.

Tests cover:
- Symbol loading from config dict
- Symbol loading from YAML file
- validate_symbols with valid/invalid codes
- get_exchange for known/unknown symbols
- reload_symbols detects added/removed symbols
- refresh_status returns expected structure
- Edge cases (empty config, missing keys, None values)
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sdk() -> MagicMock:
    """Return a mock Fubon SDK."""
    return MagicMock()


def _make_config(symbols: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return a minimal config dict with the given symbols."""
    if symbols is None:
        symbols = [
            {"code": "2330", "exchange": "TSE"},
            {"code": "2317", "exchange": "TSE"},
            {"code": "TXFC6", "exchange": "FUT"},
        ]
    return {"symbols": symbols}


def _make_runtime(
    config: dict[str, Any] | None = None,
    config_path: str | None = None,
):
    from hft_platform.feed_adapter.fubon.contracts_runtime import (
        FubonContractsRuntime,
    )

    if config is None and config_path is None:
        config = _make_config()
    return FubonContractsRuntime(_make_sdk(), config_path=config_path, config=config)


# ---------------------------------------------------------------------------
# Symbol loading from config dict
# ---------------------------------------------------------------------------


class TestLoadFromConfig:
    def test_loads_symbols_from_config_dict(self) -> None:
        rt = _make_runtime()
        assert len(rt.symbols) == 3
        codes = [s["code"] for s in rt.symbols]
        assert "2330" in codes
        assert "2317" in codes
        assert "TXFC6" in codes

    def test_empty_config_yields_empty_symbols(self) -> None:
        rt = _make_runtime(config={"symbols": []})
        assert rt.symbols == []

    def test_missing_symbols_key_yields_empty(self) -> None:
        rt = _make_runtime(config={"other_key": 123})
        assert rt.symbols == []

    def test_none_config_yields_empty(self) -> None:
        from hft_platform.feed_adapter.fubon.contracts_runtime import (
            FubonContractsRuntime,
        )

        rt = FubonContractsRuntime(_make_sdk(), config_path=None, config=None)
        assert rt.symbols == []

    def test_symbols_without_code_are_skipped_in_map(self) -> None:
        config = _make_config([{"exchange": "TSE"}, {"code": "2330", "exchange": "TSE"}])
        rt = _make_runtime(config=config)
        assert len(rt.symbols) == 2
        assert "2330" in rt._code_exchange_map
        assert len(rt._code_exchange_map) == 1


# ---------------------------------------------------------------------------
# Symbol loading from YAML file
# ---------------------------------------------------------------------------


class TestLoadFromYaml:
    def test_loads_symbols_from_yaml_file(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            symbols:
              - code: "1101"
                exchange: TSE
              - code: "MXFC0"
                exchange: FUT
        """)
        yaml_file = tmp_path / "symbols.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")

        rt = _make_runtime(config_path=str(yaml_file))
        assert len(rt.symbols) == 2
        assert rt.get_exchange("1101") == "TSE"
        assert rt.get_exchange("MXFC0") == "FUT"

    def test_yaml_takes_priority_over_config(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            symbols:
              - code: "9999"
                exchange: OTC
        """)
        yaml_file = tmp_path / "symbols.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")

        config = _make_config([{"code": "2330", "exchange": "TSE"}])
        rt = _make_runtime(config=config, config_path=str(yaml_file))
        # YAML should take priority
        assert len(rt.symbols) == 1
        assert rt.symbols[0]["code"] == "9999"

    def test_missing_yaml_falls_back_to_config(self) -> None:
        config = _make_config([{"code": "2330", "exchange": "TSE"}])
        rt = _make_runtime(config=config, config_path="/nonexistent/symbols.yaml")
        assert len(rt.symbols) == 1
        assert rt.symbols[0]["code"] == "2330"

    def test_invalid_yaml_falls_back_to_config(self, tmp_path: Path) -> None:
        yaml_file = tmp_path / "bad.yaml"
        yaml_file.write_text(":::invalid yaml:::", encoding="utf-8")

        config = _make_config([{"code": "2330", "exchange": "TSE"}])
        rt = _make_runtime(config=config, config_path=str(yaml_file))
        assert len(rt.symbols) == 1


# ---------------------------------------------------------------------------
# validate_symbols
# ---------------------------------------------------------------------------


class TestValidateSymbols:
    def test_all_valid_returns_empty(self) -> None:
        rt = _make_runtime()
        assert rt.validate_symbols() == []

    def test_detects_invalid_codes(self) -> None:
        config = _make_config([
            {"code": "2330", "exchange": "TSE"},
            {"code": "bad symbol!", "exchange": "TSE"},
            {"code": "has space", "exchange": "TSE"},
            {"code": "", "exchange": "TSE"},
        ])
        rt = _make_runtime(config=config)
        invalid = rt.validate_symbols()
        assert "bad symbol!" in invalid
        assert "has space" in invalid
        assert "" in invalid
        assert "2330" not in invalid

    def test_skips_entries_with_no_code(self) -> None:
        config = _make_config([{"exchange": "TSE"}])
        rt = _make_runtime(config=config)
        assert rt.validate_symbols() == []


# ---------------------------------------------------------------------------
# get_exchange
# ---------------------------------------------------------------------------


class TestGetExchange:
    def test_known_symbol(self) -> None:
        rt = _make_runtime()
        assert rt.get_exchange("2330") == "TSE"
        assert rt.get_exchange("TXFC6") == "FUT"

    def test_unknown_symbol_returns_empty_string(self) -> None:
        rt = _make_runtime()
        assert rt.get_exchange("UNKNOWN") == ""

    def test_symbol_without_exchange_returns_empty_string(self) -> None:
        config = _make_config([{"code": "9999"}])
        rt = _make_runtime(config=config)
        assert rt.get_exchange("9999") == ""


# ---------------------------------------------------------------------------
# reload_symbols
# ---------------------------------------------------------------------------


class TestReloadSymbols:
    def test_reload_detects_added_symbols(self) -> None:
        config_initial = _make_config([{"code": "2330", "exchange": "TSE"}])
        rt = _make_runtime(config=config_initial)
        assert len(rt.symbols) == 1

        config_new = _make_config([
            {"code": "2330", "exchange": "TSE"},
            {"code": "2317", "exchange": "TSE"},
        ])
        rt.reload_symbols(config=config_new)
        assert len(rt.symbols) == 2
        assert rt.get_exchange("2317") == "TSE"

    def test_reload_detects_removed_symbols(self) -> None:
        config_initial = _make_config([
            {"code": "2330", "exchange": "TSE"},
            {"code": "2317", "exchange": "TSE"},
        ])
        rt = _make_runtime(config=config_initial)
        assert len(rt.symbols) == 2

        config_new = _make_config([{"code": "2330", "exchange": "TSE"}])
        rt.reload_symbols(config=config_new)
        assert len(rt.symbols) == 1
        assert rt.get_exchange("2317") == ""

    def test_reload_no_changes(self) -> None:
        config = _make_config([{"code": "2330", "exchange": "TSE"}])
        rt = _make_runtime(config=config)
        rt.reload_symbols(config=config)
        assert len(rt.symbols) == 1

    def test_reload_from_yaml(self, tmp_path: Path) -> None:
        yaml_v1 = textwrap.dedent("""\
            symbols:
              - code: "2330"
                exchange: TSE
        """)
        yaml_file = tmp_path / "symbols.yaml"
        yaml_file.write_text(yaml_v1, encoding="utf-8")
        rt = _make_runtime(config_path=str(yaml_file))
        assert len(rt.symbols) == 1

        yaml_v2 = textwrap.dedent("""\
            symbols:
              - code: "2330"
                exchange: TSE
              - code: "1101"
                exchange: TSE
        """)
        yaml_file.write_text(yaml_v2, encoding="utf-8")
        rt.reload_symbols()
        assert len(rt.symbols) == 2
        assert rt.get_exchange("1101") == "TSE"


# ---------------------------------------------------------------------------
# refresh_status
# ---------------------------------------------------------------------------


class TestRefreshStatus:
    def test_returns_expected_structure(self) -> None:
        rt = _make_runtime()
        status = rt.refresh_status()
        assert status["status"] == "ok"
        assert status["source"] == "config"
        assert status["symbol_count"] == 3

    def test_source_is_yaml_when_config_path_set(self, tmp_path: Path) -> None:
        yaml_content = textwrap.dedent("""\
            symbols:
              - code: "2330"
                exchange: TSE
        """)
        yaml_file = tmp_path / "symbols.yaml"
        yaml_file.write_text(yaml_content, encoding="utf-8")

        rt = _make_runtime(config_path=str(yaml_file))
        status = rt.refresh_status()
        assert status["source"] == "yaml"
        assert status["symbol_count"] == 1

    def test_empty_symbols_still_returns_ok(self) -> None:
        rt = _make_runtime(config={"symbols": []})
        status = rt.refresh_status()
        assert status["status"] == "ok"
        assert status["symbol_count"] == 0
