"""Tests for ShioajiClientConfig and load_shioaji_config."""

from __future__ import annotations

import dataclasses
import os
from unittest import mock

import pytest

from hft_platform.feed_adapter.shioaji._config import (
    ShioajiClientConfig,
    _as_bool,
    load_shioaji_config,
)

# ---------------------------------------------------------------------------
# _as_bool helper
# ---------------------------------------------------------------------------

class TestAsBool:
    def test_none_returns_false(self) -> None:
        assert _as_bool(None) is False

    def test_true_bool(self) -> None:
        assert _as_bool(True) is True

    def test_false_bool(self) -> None:
        assert _as_bool(False) is False

    @pytest.mark.parametrize("val", ["1", "true", "True", "YES", "on", " On "])
    def test_truthy_strings(self, val: str) -> None:
        assert _as_bool(val) is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "random"])
    def test_falsy_strings(self, val: str) -> None:
        assert _as_bool(val) is False


# ---------------------------------------------------------------------------
# ShioajiClientConfig dataclass
# ---------------------------------------------------------------------------

class TestShioajiClientConfig:
    def test_is_frozen(self) -> None:
        cfg = ShioajiClientConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.simulation = True  # type: ignore[misc]

    def test_has_slots(self) -> None:
        assert hasattr(ShioajiClientConfig, "__slots__")

    def test_defaults(self) -> None:
        cfg = ShioajiClientConfig()
        assert cfg.max_subscriptions == 200
        assert cfg.simulation is False
        assert cfg.quote_version == "v1"
        assert cfg.quote_version_mode == "auto"
        assert cfg.api_soft_cap == 20
        assert cfg.session_refresh_interval_s == 86400.0
        assert cfg.contract_cache_path == "config/contracts.json"


# ---------------------------------------------------------------------------
# load_shioaji_config
# ---------------------------------------------------------------------------

class TestLoadShioajiConfig:
    def test_defaults_no_env(self) -> None:
        """With no env vars set, defaults should be sensible."""
        env_patch = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("HFT_", "SHIOAJI_", "CA_", "SYMBOLS_"))
        }
        with mock.patch.dict(os.environ, env_patch, clear=True):
            cfg = load_shioaji_config()
        assert cfg.simulation is False
        assert cfg.quote_version_mode == "auto"
        assert cfg.quote_version == "v1"
        assert cfg.activate_ca is False
        assert cfg.session_lock_enabled is True

    def test_simulation_from_env(self) -> None:
        with mock.patch.dict(os.environ, {"HFT_MODE": "sim"}, clear=False):
            cfg = load_shioaji_config()
        assert cfg.simulation is True
        # CA should be disabled in simulation
        assert cfg.activate_ca is False

    def test_simulation_from_settings_dict(self) -> None:
        cfg = load_shioaji_config(settings={"simulation": True})
        assert cfg.simulation is True

    def test_ca_from_settings_dict(self) -> None:
        with mock.patch.dict(os.environ, {"HFT_MODE": "real"}, clear=False):
            cfg = load_shioaji_config(settings={
                "activate_ca": "1",
                "ca_path": "/tmp/cert.p12",
                "ca_password": "secret",
            })
        assert cfg.activate_ca is True
        assert cfg.ca_path == "/tmp/cert.p12"
        assert cfg.ca_password == "secret"

    def test_ca_password_env_indirection(self) -> None:
        with mock.patch.dict(os.environ, {"MY_CA_PASS": "s3cret", "HFT_MODE": "real"}, clear=False):
            cfg = load_shioaji_config(settings={"ca_password_env": "MY_CA_PASS"})
        assert cfg.ca_password == "s3cret"

    def test_quote_version_v0(self) -> None:
        with mock.patch.dict(os.environ, {"HFT_QUOTE_VERSION": "v0"}, clear=False):
            cfg = load_shioaji_config()
        assert cfg.quote_version_mode == "v0"
        assert cfg.quote_version == "v0"

    def test_quote_version_invalid_falls_back(self) -> None:
        with mock.patch.dict(os.environ, {"HFT_QUOTE_VERSION": "v99"}, clear=False):
            cfg = load_shioaji_config()
        assert cfg.quote_version_mode == "auto"
        assert cfg.quote_version == "v1"

    def test_config_path_explicit(self) -> None:
        cfg = load_shioaji_config(config_path="/custom/path.yaml")
        assert cfg.config_path == "/custom/path.yaml"

    def test_config_path_from_env(self) -> None:
        with mock.patch.dict(os.environ, {"SYMBOLS_CONFIG": "/env/symbols.yaml"}, clear=False):
            cfg = load_shioaji_config()
        assert cfg.config_path == "/env/symbols.yaml"

    def test_env_int_parsing(self) -> None:
        with mock.patch.dict(os.environ, {
            "HFT_SHIOAJI_API_SOFT_CAP": "50",
            "HFT_SHIOAJI_API_HARD_CAP": "60",
            "HFT_SHIOAJI_API_WINDOW_S": "10",
        }, clear=False):
            cfg = load_shioaji_config()
        assert cfg.api_soft_cap == 50
        assert cfg.api_hard_cap == 60
        assert cfg.api_window_s == 10

    def test_env_float_parsing(self) -> None:
        with mock.patch.dict(os.environ, {
            "HFT_RECONNECT_BACKOFF_S": "5.5",
        }, clear=False):
            cfg = load_shioaji_config()
        assert cfg.reconnect_backoff_s == 5.5

    def test_session_lock_path_uses_account(self) -> None:
        with mock.patch.dict(os.environ, {"SHIOAJI_ACCOUNT": "ACC123"}, clear=False):
            cfg = load_shioaji_config()
        assert "ACC123" in cfg.session_lock_path

    def test_session_lock_path_sanitizes_special_chars(self) -> None:
        with mock.patch.dict(os.environ, {"SHIOAJI_ACCOUNT": "user@domain/bad"}, clear=False):
            cfg = load_shioaji_config()
        # Special chars replaced with underscore
        assert "@" not in cfg.session_lock_path
        assert "/" not in cfg.session_lock_path.split("shioaji_session_")[1].replace(".lock", "")
