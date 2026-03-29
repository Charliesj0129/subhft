"""Unit tests for hft_platform.config._symbols_contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from hft_platform.config._symbols_contracts import (
    load_contract_cache,
    load_metrics_cache,
    preview_lines,
    validate_symbols,
    write_contract_cache,
    write_symbols_yaml,
)
from hft_platform.config._symbols_types import (
    DEFAULT_METRICS_ENV,
    ContractIndex,
    SymbolBuildResult,
)


# ---------------------------------------------------------------------------
# load_metrics_cache
# ---------------------------------------------------------------------------


class TestLoadMetricsCache:
    def test_returns_empty_dict_when_path_is_empty_string(self):
        result = load_metrics_cache(path="")
        assert result == {}

    def test_returns_empty_dict_when_file_does_not_exist(self, tmp_path):
        result = load_metrics_cache(path=str(tmp_path / "nonexistent.json"))
        assert result == {}

    def test_loads_json_dict_without_wrapper(self, tmp_path):
        data = {"2330": {"price": 850}, "0050": {"price": 150}}
        p = tmp_path / "metrics.json"
        p.write_text(json.dumps(data))
        result = load_metrics_cache(path=str(p))
        assert result == {"2330": {"price": 850}, "0050": {"price": 150}}

    def test_loads_json_dict_with_metrics_wrapper(self, tmp_path):
        data = {"metrics": {"2330": {"price": 850}}}
        p = tmp_path / "metrics.json"
        p.write_text(json.dumps(data))
        result = load_metrics_cache(path=str(p))
        assert result == {"2330": {"price": 850}}

    def test_loads_yaml_file(self, tmp_path):
        data = {"2330": {"price": 851}}
        p = tmp_path / "metrics.yaml"
        p.write_text(yaml.safe_dump(data))
        result = load_metrics_cache(path=str(p))
        assert result == {"2330": {"price": 851}}

    def test_loads_yml_file(self, tmp_path):
        data = {"0050": {"price": 155}}
        p = tmp_path / "metrics.yml"
        p.write_text(yaml.safe_dump(data))
        result = load_metrics_cache(path=str(p))
        assert result == {"0050": {"price": 155}}

    def test_non_dict_payload_wrapped_as_value(self, tmp_path):
        data = {"2330": 850}
        p = tmp_path / "metrics.json"
        p.write_text(json.dumps(data))
        result = load_metrics_cache(path=str(p))
        assert result == {"2330": {"value": 850}}

    def test_list_of_dicts_with_code_key(self, tmp_path):
        data = [{"code": "2330", "price": 850}, {"code": "0050", "price": 150}]
        p = tmp_path / "metrics.json"
        p.write_text(json.dumps(data))
        result = load_metrics_cache(path=str(p))
        assert "2330" in result
        assert result["2330"] == {"price": 850}
        assert "0050" in result

    def test_list_of_dicts_with_symbol_key(self, tmp_path):
        data = [{"symbol": "TXFD6", "oi": 12345}]
        p = tmp_path / "metrics.json"
        p.write_text(json.dumps(data))
        result = load_metrics_cache(path=str(p))
        assert "TXFD6" in result
        assert result["TXFD6"] == {"oi": 12345}

    def test_list_skips_items_without_code_or_symbol(self, tmp_path):
        data = [{"price": 850}, {"code": "2330", "price": 851}]
        p = tmp_path / "metrics.json"
        p.write_text(json.dumps(data))
        result = load_metrics_cache(path=str(p))
        assert list(result.keys()) == ["2330"]

    def test_list_skips_non_dict_items(self, tmp_path):
        data = ["bad_item", {"code": "2330", "price": 850}]
        p = tmp_path / "metrics.json"
        p.write_text(json.dumps(data))
        result = load_metrics_cache(path=str(p))
        assert "2330" in result

    def test_empty_or_whitespace_code_skipped(self, tmp_path):
        data = {"": {"price": 1}, "  ": {"price": 2}, "2330": {"price": 3}}
        p = tmp_path / "metrics.json"
        p.write_text(json.dumps(data))
        result = load_metrics_cache(path=str(p))
        assert "" not in result
        assert "  " not in result
        assert "2330" in result

    def test_corrupt_file_returns_empty_dict(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not valid json {{{")
        result = load_metrics_cache(path=str(p))
        assert result == {}


# ---------------------------------------------------------------------------
# load_contract_cache
# ---------------------------------------------------------------------------


class TestLoadContractCache:
    def test_returns_none_when_path_empty(self):
        result = load_contract_cache(path="")
        assert result is None

    def test_returns_none_when_file_missing(self, tmp_path):
        result = load_contract_cache(path=str(tmp_path / "nope.json"))
        assert result is None

    def test_loads_json_with_contracts_wrapper(self, tmp_path):
        data = {"contracts": [{"code": "2330", "exchange": "TSE"}]}
        p = tmp_path / "contracts.json"
        p.write_text(json.dumps(data))
        result = load_contract_cache(path=str(p), metrics_path="")
        assert isinstance(result, ContractIndex)
        assert "2330" in result.by_code

    def test_loads_bare_list(self, tmp_path):
        data = [{"code": "0050", "exchange": "TSE"}]
        p = tmp_path / "contracts.json"
        p.write_text(json.dumps(data))
        result = load_contract_cache(path=str(p), metrics_path="")
        assert isinstance(result, ContractIndex)
        assert "0050" in result.by_code

    def test_loads_yaml_file(self, tmp_path):
        data = [{"code": "TXFD6", "exchange": "TAIFEX", "type": "future"}]
        p = tmp_path / "contracts.yaml"
        p.write_text(yaml.safe_dump(data))
        result = load_contract_cache(path=str(p), metrics_path="")
        assert isinstance(result, ContractIndex)
        assert "TXFD6" in result.by_code

    def test_explicit_metrics_path_loaded(self, tmp_path):
        contracts = [{"code": "2330", "exchange": "TSE"}]
        metrics = {"2330": {"price": 900}}
        cp = tmp_path / "contracts.json"
        mp = tmp_path / "metrics.json"
        cp.write_text(json.dumps(contracts))
        mp.write_text(json.dumps(metrics))
        result = load_contract_cache(path=str(cp), metrics_path=str(mp))
        assert result is not None
        assert result.metrics_by_code.get("2330", {}).get("price") == 900

    def test_env_var_fallback_for_metrics(self, tmp_path, monkeypatch):
        contracts = [{"code": "2330", "exchange": "TSE"}]
        metrics = {"2330": {"price": 901}}
        cp = tmp_path / "contracts.json"
        mp = tmp_path / "metrics.json"
        cp.write_text(json.dumps(contracts))
        mp.write_text(json.dumps(metrics))
        monkeypatch.setenv(DEFAULT_METRICS_ENV, str(mp))
        result = load_contract_cache(path=str(cp))
        assert result is not None
        assert result.metrics_by_code.get("2330", {}).get("price") == 901

    def test_corrupt_file_returns_none(self, tmp_path):
        p = tmp_path / "contracts.json"
        p.write_text("{not json")
        result = load_contract_cache(path=str(p), metrics_path="")
        assert result is None

    def test_empty_metrics_path_skips_metrics(self, tmp_path):
        data = [{"code": "2330"}]
        p = tmp_path / "contracts.json"
        p.write_text(json.dumps(data))
        result = load_contract_cache(path=str(p), metrics_path="")
        assert result is not None
        assert result.metrics_by_code == {}


# ---------------------------------------------------------------------------
# write_contract_cache
# ---------------------------------------------------------------------------


class TestWriteContractCache:
    def test_creates_new_file_with_version_1(self, tmp_path):
        dest = tmp_path / "contracts.json"
        contracts = [{"code": "2330"}]
        write_contract_cache(contracts, path=str(dest))
        data = json.loads(dest.read_text())
        assert data["cache_version"] == 1
        assert data["contracts"] == contracts
        assert "updated_at" in data

    def test_increments_version_on_subsequent_writes(self, tmp_path):
        dest = tmp_path / "contracts.json"
        write_contract_cache([{"code": "A"}], path=str(dest))
        write_contract_cache([{"code": "B"}], path=str(dest))
        data = json.loads(dest.read_text())
        assert data["cache_version"] == 2

    def test_creates_parent_directories(self, tmp_path):
        dest = tmp_path / "sub" / "dir" / "contracts.json"
        write_contract_cache([{"code": "X"}], path=str(dest))
        assert dest.exists()

    def test_no_tmp_file_left_behind(self, tmp_path):
        dest = tmp_path / "contracts.json"
        write_contract_cache([{"code": "X"}], path=str(dest))
        assert not (tmp_path / "contracts.json.tmp").exists()

    def test_handles_corrupt_existing_file_gracefully(self, tmp_path):
        dest = tmp_path / "contracts.json"
        dest.write_text("{{corrupt}}")
        # Should not raise; version should default to 1
        write_contract_cache([{"code": "2330"}], path=str(dest))
        data = json.loads(dest.read_text())
        assert data["cache_version"] == 1


# ---------------------------------------------------------------------------
# write_symbols_yaml
# ---------------------------------------------------------------------------


class TestWriteSymbolsYaml:
    def test_writes_yaml_with_symbols_key(self, tmp_path):
        dest = tmp_path / "symbols.yaml"
        symbols = [{"code": "2330", "exchange": "TSE"}]
        write_symbols_yaml(symbols, output_path=str(dest))
        loaded = yaml.safe_load(dest.read_text())
        assert loaded == {"symbols": symbols}

    def test_creates_parent_directories(self, tmp_path):
        dest = tmp_path / "nested" / "symbols.yaml"
        write_symbols_yaml([], output_path=str(dest))
        assert dest.exists()

    def test_no_tmp_file_left_behind(self, tmp_path):
        dest = tmp_path / "symbols.yaml"
        write_symbols_yaml([], output_path=str(dest))
        assert not (tmp_path / "symbols.yaml.tmp").exists()

    def test_roundtrip_preserves_all_fields(self, tmp_path):
        dest = tmp_path / "symbols.yaml"
        symbols = [{"code": "2330", "exchange": "TSE", "tick_size": 0.5, "price_scale": 10000}]
        write_symbols_yaml(symbols, output_path=str(dest))
        loaded = yaml.safe_load(dest.read_text())
        assert loaded["symbols"] == symbols


# ---------------------------------------------------------------------------
# validate_symbols
# ---------------------------------------------------------------------------


class TestValidateSymbols:
    def test_happy_path_no_errors(self):
        symbols = [{"code": "2330", "exchange": "TSE"}]
        result = validate_symbols(symbols)
        assert result.ok()
        assert result.errors == []

    def test_missing_code_adds_error(self):
        symbols = [{"exchange": "TSE"}]
        result = validate_symbols(symbols)
        assert not result.ok()
        assert any("missing code" in e for e in result.errors)

    def test_duplicate_code_adds_error(self):
        symbols = [
            {"code": "2330", "exchange": "TSE"},
            {"code": "2330", "exchange": "TSE"},
        ]
        result = validate_symbols(symbols)
        assert not result.ok()
        assert any("Duplicate" in e for e in result.errors)

    def test_missing_exchange_adds_error(self):
        symbols = [{"code": "2330"}]
        result = validate_symbols(symbols)
        assert not result.ok()
        assert any("Missing exchange" in e for e in result.errors)

    def test_unknown_exchange_adds_error(self):
        symbols = [{"code": "2330", "exchange": "NASDAQ"}]
        result = validate_symbols(symbols)
        assert not result.ok()
        assert any("Unknown exchange" in e for e in result.errors)

    def test_valid_exchanges_accepted(self):
        valid_exchanges = ["TSE", "OTC", "FUT", "TAIFEX", "SIM"]
        for ex in valid_exchanges:
            symbols = [{"code": "TEST", "exchange": ex}]
            result = validate_symbols(symbols)
            exchange_errors = [e for e in result.errors if "Unknown exchange" in e]
            assert exchange_errors == [], f"Exchange {ex} should be valid"

    def test_negative_tick_size_adds_error(self):
        symbols = [{"code": "2330", "exchange": "TSE", "tick_size": -1}]
        result = validate_symbols(symbols)
        assert not result.ok()
        assert any("tick_size" in e for e in result.errors)

    def test_zero_tick_size_adds_error(self):
        symbols = [{"code": "2330", "exchange": "TSE", "tick_size": 0}]
        result = validate_symbols(symbols)
        assert not result.ok()

    def test_non_numeric_tick_size_adds_error(self):
        symbols = [{"code": "2330", "exchange": "TSE", "tick_size": "bad"}]
        result = validate_symbols(symbols)
        assert not result.ok()

    def test_negative_price_scale_adds_error(self):
        symbols = [{"code": "2330", "exchange": "TSE", "price_scale": -10000}]
        result = validate_symbols(symbols)
        assert not result.ok()
        assert any("price_scale" in e for e in result.errors)

    def test_zero_price_scale_adds_error(self):
        symbols = [{"code": "2330", "exchange": "TSE", "price_scale": 0}]
        result = validate_symbols(symbols)
        assert not result.ok()

    def test_non_numeric_price_scale_adds_error(self):
        symbols = [{"code": "2330", "exchange": "TSE", "price_scale": "oops"}]
        result = validate_symbols(symbols)
        assert not result.ok()

    def test_subscription_limit_exceeded_adds_error(self):
        symbols = [{"code": str(i), "exchange": "TSE"} for i in range(5)]
        result = validate_symbols(symbols, max_subscriptions=3)
        assert not result.ok()
        assert any("subscription limit" in e for e in result.errors)

    def test_subscription_limit_at_boundary_ok(self):
        symbols = [{"code": str(i), "exchange": "TSE"} for i in range(3)]
        result = validate_symbols(symbols, max_subscriptions=3)
        limit_errors = [e for e in result.errors if "subscription limit" in e]
        assert limit_errors == []

    def test_contract_index_rejects_unknown_code(self):
        symbols = [{"code": "UNKNOWN", "exchange": "TSE"}]
        contract_index = ContractIndex(contracts=[{"code": "2330"}])
        result = validate_symbols(symbols, contract_index=contract_index)
        assert not result.ok()
        assert any("Unsubscribable" in e for e in result.errors)

    def test_contract_index_allows_sim_exchange(self):
        symbols = [{"code": "SIM_STOCK", "exchange": "SIM"}]
        contract_index = ContractIndex(contracts=[])
        result = validate_symbols(symbols, contract_index=contract_index)
        unsubscribable_errors = [e for e in result.errors if "Unsubscribable" in e]
        assert unsubscribable_errors == []

    def test_contract_index_allows_known_code(self):
        symbols = [{"code": "2330", "exchange": "TSE"}]
        contract_index = ContractIndex(contracts=[{"code": "2330"}])
        result = validate_symbols(symbols, contract_index=contract_index)
        unsubscribable_errors = [e for e in result.errors if "Unsubscribable" in e]
        assert unsubscribable_errors == []

    def test_tick_size_none_is_skipped(self):
        symbols = [{"code": "2330", "exchange": "TSE", "tick_size": None}]
        result = validate_symbols(symbols)
        tick_errors = [e for e in result.errors if "tick_size" in e]
        assert tick_errors == []

    def test_price_scale_none_is_skipped(self):
        symbols = [{"code": "2330", "exchange": "TSE", "price_scale": None}]
        result = validate_symbols(symbols)
        scale_errors = [e for e in result.errors if "price_scale" in e]
        assert scale_errors == []


# ---------------------------------------------------------------------------
# preview_lines
# ---------------------------------------------------------------------------


class TestPreviewLines:
    def test_shows_symbol_count(self):
        result = SymbolBuildResult(symbols=[{"code": "2330", "exchange": "TSE"}])
        lines = preview_lines(result)
        assert any("symbols=1" in line for line in lines)

    def test_shows_sample_codes(self):
        result = SymbolBuildResult(symbols=[{"code": "2330", "exchange": "TSE"}])
        lines = preview_lines(result)
        assert any("2330" in line for line in lines)

    def test_shows_errors_and_warnings_line_when_present(self):
        result = SymbolBuildResult(
            symbols=[],
            errors=["bad thing"],
            warnings=["check this"],
        )
        lines = preview_lines(result)
        assert any("errors=1" in line and "warnings=1" in line for line in lines)

    def test_no_errors_warnings_line_when_clean(self):
        result = SymbolBuildResult(symbols=[{"code": "2330", "exchange": "TSE"}])
        lines = preview_lines(result)
        error_lines = [line for line in lines if "errors=" in line]
        assert error_lines == []

    def test_empty_symbols_list(self):
        result = SymbolBuildResult(symbols=[])
        lines = preview_lines(result)
        assert any("symbols=0" in line for line in lines)

    def test_sample_limit_applied(self):
        symbols = [{"code": str(i), "exchange": "TSE"} for i in range(20)]
        result = SymbolBuildResult(symbols=symbols)
        lines = preview_lines(result, sample=3)
        # Only the first 3 should appear in sample line
        sample_line = next((l for l in lines if "sample=" in l), "")
        assert sample_line.count("(") == 3

    def test_symbols_without_code_skipped_in_sample(self):
        symbols = [{"exchange": "TSE"}, {"code": "2330", "exchange": "TSE"}]
        result = SymbolBuildResult(symbols=symbols)
        lines = preview_lines(result, sample=10)
        sample_line = next((l for l in lines if "sample=" in l), "")
        assert "2330" in sample_line
