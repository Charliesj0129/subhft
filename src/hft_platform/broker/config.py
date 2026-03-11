from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


@dataclass(slots=True, frozen=True)
class BrokerAuthConfig:
    method: str  # "cert" | "apikey"
    env_api_key: str
    env_secret_key: str = ""
    env_password: str = ""
    requires_ca_cert: bool = False


@dataclass(slots=True, frozen=True)
class BrokerTransportConfig:
    protocol: str = ""  # "proprietary" | "http_ws"
    sdk_package: str = ""
    ws_url: str = ""
    rest_url: str = ""
    timeout_s: float = 3.0


@dataclass(slots=True, frozen=True)
class BrokerRateLimits:
    soft_cap: int = 100
    hard_cap: int = 150
    window_seconds: int = 10


@dataclass(slots=True, frozen=True)
class BrokerCapabilitiesConfig:
    batch_orders: bool = False
    smart_orders: bool = False
    l2_depth: bool = True
    max_custom_field_len: int = 6


@dataclass(slots=True, frozen=True)
class BrokerLatencyProfile:
    place_order_p95_ms: float | None = None
    update_order_p95_ms: float | None = None
    cancel_order_p95_ms: float | None = None


@dataclass(slots=True, frozen=True)
class BrokerConfig:
    name: str
    display_name: str = ""
    auth: BrokerAuthConfig = field(
        default_factory=lambda: BrokerAuthConfig(method="cert", env_api_key=""),
    )
    transport: BrokerTransportConfig = field(default_factory=BrokerTransportConfig)
    rate_limits: BrokerRateLimits = field(default_factory=BrokerRateLimits)
    capabilities: BrokerCapabilitiesConfig = field(
        default_factory=BrokerCapabilitiesConfig,
    )
    latency_profile: BrokerLatencyProfile = field(
        default_factory=BrokerLatencyProfile,
    )


def load_broker_config(
    broker_name: str,
    config_dir: Path | None = None,
) -> BrokerConfig:
    """Load broker config from YAML file."""
    if config_dir is None:
        config_dir = Path("config/base/brokers")

    config_path = config_dir / f"{broker_name}.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Broker config not found: {config_path}")

    if yaml is None:
        raise ImportError("PyYAML required for broker config loading")

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    broker_data = raw.get("broker", raw)

    return BrokerConfig(
        name=broker_data["name"],
        display_name=broker_data.get("display_name", ""),
        auth=BrokerAuthConfig(**broker_data.get("auth", {})),
        transport=BrokerTransportConfig(**broker_data.get("transport", {})),
        rate_limits=BrokerRateLimits(**broker_data.get("rate_limits", {})),
        capabilities=BrokerCapabilitiesConfig(
            **broker_data.get("capabilities", {}),
        ),
        latency_profile=BrokerLatencyProfile(
            **{k: v for k, v in broker_data.get("latency_profile", {}).items() if v is not None},
        ),
    )
