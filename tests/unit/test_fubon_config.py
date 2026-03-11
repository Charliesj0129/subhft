"""Tests for Fubon broker config and constant mappings."""

from __future__ import annotations

import os
from unittest import mock

import pytest

from hft_platform.feed_adapter.fubon._config import (
    load_fubon_config,
)
from hft_platform.feed_adapter.fubon._constants import (
    ACTION_MAP,
    ORDER_STATUS_MAP,
    ORDER_TYPE_MAP,
    PRICE_TYPE_MAP,
    TIF_MAP,
    resolve_fubon_enum,
)


class TestLoadFubonConfig:
    """Verify env-var parsing, dict overlay, and defaults."""

    def test_defaults_when_nothing_set(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = load_fubon_config()
        assert cfg.user_id == ""
        assert cfg.password == ""
        assert cfg.cert_path == ""
        assert cfg.cert_password == ""
        assert cfg.simulation is True
        assert cfg.realtime_mode == "Speed"
        assert cfg.order_rate_limit == 10
        assert cfg.reconnect_max_retries == 5
        assert cfg.reconnect_backoff_s == 2.0

    def test_env_vars_populate_config(self) -> None:
        env = {
            "FUBON_ID": "A123456789",
            "FUBON_PASSWORD": "s3cret",
            "FUBON_CERT_PATH": "/certs/fubon.pfx",
            "FUBON_CERT_PASSWORD": "certpw",
            "FUBON_SIMULATION": "0",
            "FUBON_REALTIME_MODE": "Normal",
            "FUBON_ORDER_RATE_LIMIT": "20",
            "FUBON_RECONNECT_MAX_RETRIES": "3",
            "FUBON_RECONNECT_BACKOFF_S": "5.5",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_fubon_config()
        assert cfg.user_id == "A123456789"
        assert cfg.password == "s3cret"
        assert cfg.cert_path == "/certs/fubon.pfx"
        assert cfg.cert_password == "certpw"
        assert cfg.simulation is False
        assert cfg.realtime_mode == "Normal"
        assert cfg.order_rate_limit == 20
        assert cfg.reconnect_max_retries == 3
        assert cfg.reconnect_backoff_s == 5.5

    def test_settings_dict_populates_config(self) -> None:
        settings = {
            "fubon": {
                "user_id": "B987654321",
                "password": "dictpw",
                "cert_path": "/dict/cert.pfx",
                "cert_password": "dictcertpw",
                "simulation": False,
                "realtime_mode": "Normal",
                "order_rate_limit": 15,
                "reconnect_max_retries": 8,
                "reconnect_backoff_s": 3.0,
            }
        }
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = load_fubon_config(settings)
        assert cfg.user_id == "B987654321"
        assert cfg.password == "dictpw"
        assert cfg.cert_path == "/dict/cert.pfx"
        assert cfg.cert_password == "dictcertpw"
        assert cfg.simulation is False
        assert cfg.realtime_mode == "Normal"
        assert cfg.order_rate_limit == 15
        assert cfg.reconnect_max_retries == 8
        assert cfg.reconnect_backoff_s == 3.0

    def test_env_vars_override_settings_dict(self) -> None:
        settings = {
            "fubon": {
                "user_id": "DICT_ID",
                "password": "dict_pw",
                "cert_path": "/dict/cert.pfx",
                "cert_password": "dict_cert_pw",
                "simulation": True,
            }
        }
        env = {
            "FUBON_ID": "ENV_ID",
            "FUBON_PASSWORD": "env_pw",
            "FUBON_CERT_PATH": "/env/cert.pfx",
            "FUBON_CERT_PASSWORD": "env_cert_pw",
            "FUBON_SIMULATION": "0",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = load_fubon_config(settings)
        assert cfg.user_id == "ENV_ID"
        assert cfg.password == "env_pw"
        assert cfg.cert_path == "/env/cert.pfx"
        assert cfg.cert_password == "env_cert_pw"
        assert cfg.simulation is False

    def test_frozen_config_cannot_be_mutated(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            cfg = load_fubon_config()
        with pytest.raises(AttributeError):
            cfg.user_id = "new"  # type: ignore[misc]

    def test_simulation_true_variants(self) -> None:
        for val in ("1", "true", "True", "yes", "YES"):
            with mock.patch.dict(os.environ, {"FUBON_SIMULATION": val}, clear=True):
                cfg = load_fubon_config()
            assert cfg.simulation is True, f"Expected True for FUBON_SIMULATION={val}"

    def test_simulation_false_variants(self) -> None:
        for val in ("0", "false", "no", "off"):
            with mock.patch.dict(os.environ, {"FUBON_SIMULATION": val}, clear=True):
                cfg = load_fubon_config()
            assert cfg.simulation is False, f"Expected False for FUBON_SIMULATION={val}"

    def test_int_parse_error_falls_back(self) -> None:
        with mock.patch.dict(os.environ, {"FUBON_ORDER_RATE_LIMIT": "not_a_number"}, clear=True):
            cfg = load_fubon_config()
        assert cfg.order_rate_limit == 10  # default

    def test_float_parse_error_falls_back(self) -> None:
        with mock.patch.dict(os.environ, {"FUBON_RECONNECT_BACKOFF_S": "bad"}, clear=True):
            cfg = load_fubon_config()
        assert cfg.reconnect_backoff_s == 2.0  # default


class TestFubonConstants:
    """Verify constant mapping tables contain expected keys."""

    def test_action_map_keys(self) -> None:
        assert "Buy" in ACTION_MAP
        assert "Sell" in ACTION_MAP
        assert len(ACTION_MAP) == 2

    def test_tif_map_keys(self) -> None:
        for key in ("ROD", "IOC", "FOK"):
            assert key in TIF_MAP
        assert len(TIF_MAP) == 3

    def test_order_type_map_keys(self) -> None:
        for key in ("stock", "odd_lot", "futures"):
            assert key in ORDER_TYPE_MAP
        assert ORDER_TYPE_MAP["stock"] == "Stock"
        assert ORDER_TYPE_MAP["odd_lot"] == "OddLot"
        assert ORDER_TYPE_MAP["futures"] == "Futures"

    def test_price_type_map_keys(self) -> None:
        for key in ("LMT", "MKT", "limit", "market", "limit_up", "limit_down"):
            assert key in PRICE_TYPE_MAP
        assert PRICE_TYPE_MAP["LMT"] == "Limit"
        assert PRICE_TYPE_MAP["MKT"] == "Market"
        assert PRICE_TYPE_MAP["limit_up"] == "LimitUp"
        assert PRICE_TYPE_MAP["limit_down"] == "LimitDown"

    def test_order_status_map_known_codes(self) -> None:
        assert ORDER_STATUS_MAP[10] == "confirmed"
        assert ORDER_STATUS_MAP[30] == "cancelled"
        assert ORDER_STATUS_MAP[90] == "failed"
        assert ORDER_STATUS_MAP[40] == "partial_cancelled"
        assert 4 in ORDER_STATUS_MAP
        assert 8 in ORDER_STATUS_MAP

    def test_resolve_fubon_enum_raises_when_sdk_missing(self) -> None:
        with pytest.raises(RuntimeError, match="Cannot resolve Fubon enum"):
            resolve_fubon_enum("fubon_neo.constant", "BSAction", "Buy")

    def test_resolve_fubon_enum_raises_on_bad_member(self) -> None:
        # Use a real module but non-existent attribute
        with pytest.raises(RuntimeError, match="Cannot resolve Fubon enum"):
            resolve_fubon_enum("os", "path", "NonExistent")
