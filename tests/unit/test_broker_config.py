from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hft_platform.broker.config import (
    BrokerAuthConfig,
    BrokerCapabilitiesConfig,
    BrokerConfig,
    BrokerLatencyProfile,
    BrokerRateLimits,
    BrokerTransportConfig,
    load_broker_config,
)

SHIOAJI_YAML = {
    "broker": {
        "name": "shioaji",
        "display_name": "永豐金證券 (Sinopac)",
        "auth": {
            "method": "cert",
            "env_api_key": "SHIOAJI_API_KEY",
            "env_secret_key": "SHIOAJI_SECRET_KEY",
            "requires_ca_cert": True,
        },
        "transport": {
            "protocol": "proprietary",
            "sdk_package": "shioaji",
        },
        "rate_limits": {
            "soft_cap": 180,
            "hard_cap": 250,
            "window_seconds": 10,
        },
        "capabilities": {
            "batch_orders": False,
            "smart_orders": False,
            "l2_depth": True,
            "max_custom_field_len": 6,
        },
        "latency_profile": {
            "place_order_p95_ms": 35.2,
            "update_order_p95_ms": 42.4,
            "cancel_order_p95_ms": 46.1,
        },
    },
}

FUBON_YAML = {
    "broker": {
        "name": "fubon",
        "display_name": "富邦證券 (Fubon)",
        "auth": {
            "method": "apikey",
            "env_api_key": "HFT_FUBON_API_KEY",
            "env_password": "HFT_FUBON_PASSWORD",
            "requires_ca_cert": False,
        },
        "transport": {
            "protocol": "http_ws",
            "sdk_package": "fubon_neo",
            "ws_url": "wss://api.fbs.com.tw/ws/v2",
            "rest_url": "https://api.fbs.com.tw/api/v2",
            "timeout_s": 3.0,
        },
        "rate_limits": {
            "soft_cap": 100,
            "hard_cap": 150,
            "window_seconds": 10,
        },
        "capabilities": {
            "batch_orders": True,
            "smart_orders": True,
            "l2_depth": True,
            "max_custom_field_len": 32,
        },
        "latency_profile": {
            "place_order_p95_ms": None,
            "update_order_p95_ms": None,
            "cancel_order_p95_ms": None,
        },
    },
}


def _write_broker_yaml(tmp_path: Path, name: str, data: dict) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.dump(data, allow_unicode=True))
    return path


class TestLoadShioajiConfig:
    def test_load_shioaji_config(self, tmp_path: Path) -> None:
        _write_broker_yaml(tmp_path, "shioaji", SHIOAJI_YAML)
        cfg = load_broker_config("shioaji", config_dir=tmp_path)

        assert cfg.name == "shioaji"
        assert cfg.display_name == "永豐金證券 (Sinopac)"
        assert cfg.auth.method == "cert"
        assert cfg.auth.env_api_key == "SHIOAJI_API_KEY"
        assert cfg.auth.env_secret_key == "SHIOAJI_SECRET_KEY"
        assert cfg.auth.requires_ca_cert is True
        assert cfg.transport.protocol == "proprietary"
        assert cfg.transport.sdk_package == "shioaji"
        assert cfg.rate_limits.soft_cap == 180
        assert cfg.rate_limits.hard_cap == 250
        assert cfg.rate_limits.window_seconds == 10
        assert cfg.capabilities.batch_orders is False
        assert cfg.capabilities.smart_orders is False
        assert cfg.capabilities.l2_depth is True
        assert cfg.capabilities.max_custom_field_len == 6
        assert cfg.latency_profile.place_order_p95_ms == pytest.approx(35.2)
        assert cfg.latency_profile.update_order_p95_ms == pytest.approx(42.4)
        assert cfg.latency_profile.cancel_order_p95_ms == pytest.approx(46.1)


class TestLoadFubonConfig:
    def test_load_fubon_config(self, tmp_path: Path) -> None:
        _write_broker_yaml(tmp_path, "fubon", FUBON_YAML)
        cfg = load_broker_config("fubon", config_dir=tmp_path)

        assert cfg.name == "fubon"
        assert cfg.display_name == "富邦證券 (Fubon)"
        assert cfg.auth.method == "apikey"
        assert cfg.auth.env_api_key == "HFT_FUBON_API_KEY"
        assert cfg.auth.env_password == "HFT_FUBON_PASSWORD"
        assert cfg.auth.requires_ca_cert is False
        assert cfg.transport.protocol == "http_ws"
        assert cfg.transport.sdk_package == "fubon_neo"
        assert cfg.transport.ws_url == "wss://api.fbs.com.tw/ws/v2"
        assert cfg.transport.rest_url == "https://api.fbs.com.tw/api/v2"
        assert cfg.transport.timeout_s == pytest.approx(3.0)
        assert cfg.rate_limits.soft_cap == 100
        assert cfg.rate_limits.hard_cap == 150
        assert cfg.capabilities.batch_orders is True
        assert cfg.capabilities.smart_orders is True
        assert cfg.capabilities.max_custom_field_len == 32


class TestMissingConfig:
    def test_missing_config_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Broker config not found"):
            load_broker_config("nonexistent", config_dir=tmp_path)


class TestBrokerConfigFrozen:
    def test_broker_config_frozen(self) -> None:
        cfg = BrokerConfig(name="test")
        with pytest.raises(AttributeError):
            cfg.name = "changed"  # type: ignore[misc]

    def test_auth_config_frozen(self) -> None:
        auth = BrokerAuthConfig(method="cert", env_api_key="KEY")
        with pytest.raises(AttributeError):
            auth.method = "apikey"  # type: ignore[misc]


class TestBrokerConfigSlots:
    def test_broker_config_slots(self) -> None:
        assert hasattr(BrokerConfig, "__slots__")

    def test_auth_config_slots(self) -> None:
        assert hasattr(BrokerAuthConfig, "__slots__")

    def test_transport_config_slots(self) -> None:
        assert hasattr(BrokerTransportConfig, "__slots__")

    def test_rate_limits_slots(self) -> None:
        assert hasattr(BrokerRateLimits, "__slots__")

    def test_capabilities_config_slots(self) -> None:
        assert hasattr(BrokerCapabilitiesConfig, "__slots__")

    def test_latency_profile_slots(self) -> None:
        assert hasattr(BrokerLatencyProfile, "__slots__")


class TestAuthConfigDefaults:
    def test_auth_config_defaults(self) -> None:
        auth = BrokerAuthConfig(method="cert", env_api_key="KEY")
        assert auth.env_secret_key == ""
        assert auth.env_password == ""
        assert auth.requires_ca_cert is False


class TestLatencyProfileNullable:
    def test_latency_profile_nullable(self, tmp_path: Path) -> None:
        _write_broker_yaml(tmp_path, "fubon", FUBON_YAML)
        cfg = load_broker_config("fubon", config_dir=tmp_path)

        assert cfg.latency_profile.place_order_p95_ms is None
        assert cfg.latency_profile.update_order_p95_ms is None
        assert cfg.latency_profile.cancel_order_p95_ms is None

    def test_default_latency_profile_is_none(self) -> None:
        profile = BrokerLatencyProfile()
        assert profile.place_order_p95_ms is None
        assert profile.update_order_p95_ms is None
        assert profile.cancel_order_p95_ms is None
