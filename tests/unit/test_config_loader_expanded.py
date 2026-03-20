"""Expanded tests for config/loader.py — covers _merge, _load_settings_py,
_env_overrides, detect_live_credentials, _load_yaml, and load_settings."""

from __future__ import annotations

import textwrap
from copy import deepcopy
from typing import Any, Dict
from unittest.mock import patch

from hft_platform.config.loader import (
    _env_overrides,
    _load_settings_py,
    _load_yaml,
    _merge,
    detect_live_credentials,
    load_settings,
)

# ---------------------------------------------------------------------------
# _merge()
# ---------------------------------------------------------------------------


class TestMerge:
    def test_flat_override(self) -> None:
        base: Dict[str, Any] = {"a": 1, "b": 2}
        result = _merge(deepcopy(base), {"b": 99})
        assert result["a"] == 1
        assert result["b"] == 99

    def test_nested_dict_merge(self) -> None:
        base: Dict[str, Any] = {"x": {"y": 1, "z": 2}}
        result = _merge(deepcopy(base), {"x": {"z": 99}})
        assert result["x"]["y"] == 1
        assert result["x"]["z"] == 99

    def test_nested_override_preserves_other_keys(self) -> None:
        base: Dict[str, Any] = {"a": {"b": 1, "c": 2}, "d": 3}
        result = _merge(deepcopy(base), {"a": {"b": 10}})
        assert result == {"a": {"b": 10, "c": 2}, "d": 3}

    def test_non_dict_overwrites_dict(self) -> None:
        base: Dict[str, Any] = {"a": {"nested": True}}
        result = _merge(deepcopy(base), {"a": "scalar"})
        assert result["a"] == "scalar"

    def test_new_key_added(self) -> None:
        base: Dict[str, Any] = {"a": 1}
        result = _merge(deepcopy(base), {"b": 2})
        assert result == {"a": 1, "b": 2}

    def test_empty_override(self) -> None:
        base: Dict[str, Any] = {"a": 1}
        result = _merge(deepcopy(base), {})
        assert result == {"a": 1}

    def test_deep_copy_safety(self) -> None:
        """Mutating the result must not affect the original override dict."""
        base: Dict[str, Any] = {"a": {"b": 1}}
        override: Dict[str, Any] = {"a": {"c": 2}}
        result = _merge(deepcopy(base), deepcopy(override))
        result["a"]["c"] = 999
        assert override["a"]["c"] == 2


# ---------------------------------------------------------------------------
# _load_settings_py()
# ---------------------------------------------------------------------------


class TestLoadSettingsPy:
    def test_get_settings_function(self, tmp_path) -> None:
        p = tmp_path / "settings.py"
        p.write_text(
            textwrap.dedent("""\
                def get_settings():
                    return {"mode": "live", "extra": 42}
            """)
        )
        result = _load_settings_py(str(p))
        assert result == {"mode": "live", "extra": 42}

    def test_uppercase_attrs_fallback(self, tmp_path) -> None:
        p = tmp_path / "settings.py"
        p.write_text(
            textwrap.dedent("""\
                MODE = "live"
                SECRET = "abc"
                _private = "ignored"
                lowercase = "ignored"
            """)
        )
        result = _load_settings_py(str(p))
        assert result["MODE"] == "live"
        assert result["SECRET"] == "abc"
        assert "_private" not in result
        assert "lowercase" not in result

    def test_file_not_found(self, tmp_path) -> None:
        result = _load_settings_py(str(tmp_path / "nonexistent.py"))
        assert result == {}

    def test_syntax_error(self, tmp_path) -> None:
        p = tmp_path / "bad.py"
        p.write_text("def oops(\n")
        result = _load_settings_py(str(p))
        assert result == {}

    def test_get_settings_raises(self, tmp_path) -> None:
        p = tmp_path / "settings.py"
        p.write_text(
            textwrap.dedent("""\
                def get_settings():
                    raise RuntimeError("boom")
            """)
        )
        result = _load_settings_py(str(p))
        assert result == {}


# ---------------------------------------------------------------------------
# _env_overrides()
# ---------------------------------------------------------------------------


class TestEnvOverrides:
    def test_hft_mode(self, monkeypatch) -> None:
        monkeypatch.setenv("HFT_MODE", "live")
        monkeypatch.delenv("HFT_SYMBOLS", raising=False)
        monkeypatch.delenv("HFT_PROM_PORT", raising=False)
        monkeypatch.delenv("HFT_ENV", raising=False)
        result = _env_overrides()
        assert result["mode"] == "live"

    def test_hft_symbols_comma_split(self, monkeypatch) -> None:
        monkeypatch.setenv("HFT_SYMBOLS", "2330,2317,2454")
        monkeypatch.delenv("HFT_MODE", raising=False)
        monkeypatch.delenv("HFT_PROM_PORT", raising=False)
        monkeypatch.delenv("HFT_ENV", raising=False)
        result = _env_overrides()
        assert result["symbols"] == ["2330", "2317", "2454"]

    def test_hft_prom_port_valid(self, monkeypatch) -> None:
        monkeypatch.setenv("HFT_PROM_PORT", "9999")
        monkeypatch.delenv("HFT_MODE", raising=False)
        monkeypatch.delenv("HFT_SYMBOLS", raising=False)
        monkeypatch.delenv("HFT_ENV", raising=False)
        result = _env_overrides()
        assert result["prometheus_port"] == 9999

    def test_hft_prom_port_invalid(self, monkeypatch) -> None:
        monkeypatch.setenv("HFT_PROM_PORT", "not_a_number")
        monkeypatch.delenv("HFT_MODE", raising=False)
        monkeypatch.delenv("HFT_SYMBOLS", raising=False)
        monkeypatch.delenv("HFT_ENV", raising=False)
        result = _env_overrides()
        assert "prometheus_port" not in result

    def test_hft_env(self, monkeypatch) -> None:
        monkeypatch.setenv("HFT_ENV", "staging")
        monkeypatch.delenv("HFT_MODE", raising=False)
        monkeypatch.delenv("HFT_SYMBOLS", raising=False)
        monkeypatch.delenv("HFT_PROM_PORT", raising=False)
        result = _env_overrides()
        assert result["env"] == "staging"

    def test_no_env_vars_set(self, monkeypatch) -> None:
        monkeypatch.delenv("HFT_MODE", raising=False)
        monkeypatch.delenv("HFT_SYMBOLS", raising=False)
        monkeypatch.delenv("HFT_PROM_PORT", raising=False)
        monkeypatch.delenv("HFT_ENV", raising=False)
        result = _env_overrides()
        assert result == {}


# ---------------------------------------------------------------------------
# detect_live_credentials()
# ---------------------------------------------------------------------------


class TestDetectLiveCredentials:
    def test_both_set(self, monkeypatch) -> None:
        monkeypatch.setenv("SHIOAJI_API_KEY", "key123")
        monkeypatch.setenv("SHIOAJI_SECRET_KEY", "secret456")
        assert detect_live_credentials() is True

    def test_missing_key(self, monkeypatch) -> None:
        monkeypatch.delenv("SHIOAJI_API_KEY", raising=False)
        monkeypatch.setenv("SHIOAJI_SECRET_KEY", "secret456")
        assert detect_live_credentials() is False

    def test_missing_secret(self, monkeypatch) -> None:
        monkeypatch.setenv("SHIOAJI_API_KEY", "key123")
        monkeypatch.delenv("SHIOAJI_SECRET_KEY", raising=False)
        assert detect_live_credentials() is False


# ---------------------------------------------------------------------------
# _load_yaml()
# ---------------------------------------------------------------------------


class TestLoadYaml:
    def test_valid_file(self, tmp_path) -> None:
        p = tmp_path / "cfg.yaml"
        p.write_text("mode: live\nsymbols:\n  - '2330'\n")
        result = _load_yaml(str(p))
        assert result == {"mode": "live", "symbols": ["2330"]}

    def test_missing_file(self, tmp_path) -> None:
        result = _load_yaml(str(tmp_path / "missing.yaml"))
        assert result == {}


# ---------------------------------------------------------------------------
# load_settings()
# ---------------------------------------------------------------------------


class TestLoadSettings:
    def test_priority_chain_cli_overrides(self, tmp_path, monkeypatch) -> None:
        """CLI overrides should win over base YAML defaults."""
        yaml_file = tmp_path / "main.yaml"
        yaml_file.write_text("mode: sim\nprometheus_port: 9090\n")
        monkeypatch.delenv("HFT_MODE", raising=False)
        monkeypatch.delenv("HFT_SYMBOLS", raising=False)
        monkeypatch.delenv("HFT_PROM_PORT", raising=False)
        monkeypatch.delenv("HFT_ENV", raising=False)

        with (
            patch("hft_platform.config.loader.DEFAULT_YAML_PATH", str(yaml_file)),
            patch("hft_platform.config.loader._load_settings_py", return_value={}),
            patch("os.path.exists", return_value=False),
        ):
            settings, defaults = load_settings({"prometheus_port": 7777, "skip_config_validation": True})

        assert settings["prometheus_port"] == 7777

    def test_skip_config_validation(self, tmp_path, monkeypatch) -> None:
        yaml_file = tmp_path / "main.yaml"
        yaml_file.write_text("mode: sim\n")
        monkeypatch.delenv("HFT_MODE", raising=False)
        monkeypatch.delenv("HFT_SYMBOLS", raising=False)
        monkeypatch.delenv("HFT_PROM_PORT", raising=False)
        monkeypatch.delenv("HFT_ENV", raising=False)
        monkeypatch.delenv("HFT_SKIP_CONFIG_VALIDATION", raising=False)

        with (
            patch("hft_platform.config.loader.DEFAULT_YAML_PATH", str(yaml_file)),
            patch("hft_platform.config.loader._load_settings_py", return_value={}),
            patch("os.path.exists", return_value=False),
        ):
            # skip_config_validation=True must not call validate_config_or_exit
            settings, defaults = load_settings({"skip_config_validation": True})
            assert settings["mode"] == "sim"
