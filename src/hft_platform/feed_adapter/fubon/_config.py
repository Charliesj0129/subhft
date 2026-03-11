"""Fubon broker configuration — single-pass frozen config from env vars."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from structlog import get_logger

logger = get_logger("fubon.config")


def _as_bool(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_bool(key: str, default: str = "0") -> bool:
    return _as_bool(os.getenv(key, default))


def _env_int(key: str, default: int, *, lo: int | None = None) -> int:
    try:
        v = int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        v = default
    if lo is not None:
        v = max(lo, v)
    return v


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class FubonClientConfig:
    """Immutable config for Fubon Neo SDK."""

    user_id: str
    password: str
    cert_path: str
    cert_password: str
    simulation: bool = True
    # WebSocket market data
    realtime_mode: str = "Speed"  # "Speed" or "Normal"
    # Rate limiting
    order_rate_limit: int = 10  # orders per second
    # Reconnection
    reconnect_max_retries: int = 5
    reconnect_backoff_s: float = 2.0


def load_fubon_config(settings: dict[str, Any] | None = None) -> FubonClientConfig:
    """Build FubonClientConfig from env vars and optional settings dict.

    Env vars take precedence over settings dict values.  When neither is
    provided the field falls back to its dataclass default (where one exists)
    or to an empty string for credential fields.
    """
    s = settings or {}
    fubon_cfg: dict[str, Any] = s.get("fubon", {})

    return FubonClientConfig(
        user_id=os.getenv("FUBON_ID", fubon_cfg.get("user_id", "")),
        password=os.getenv("FUBON_PASSWORD", fubon_cfg.get("password", "")),
        cert_path=os.getenv("FUBON_CERT_PATH", fubon_cfg.get("cert_path", "")),
        cert_password=os.getenv("FUBON_CERT_PASSWORD", fubon_cfg.get("cert_password", "")),
        simulation=_env_bool("FUBON_SIMULATION", str(fubon_cfg.get("simulation", True))),
        realtime_mode=os.getenv("FUBON_REALTIME_MODE", fubon_cfg.get("realtime_mode", "Speed")),
        order_rate_limit=_env_int(
            "FUBON_ORDER_RATE_LIMIT",
            int(fubon_cfg.get("order_rate_limit", 10)),
            lo=1,
        ),
        reconnect_max_retries=_env_int(
            "FUBON_RECONNECT_MAX_RETRIES",
            int(fubon_cfg.get("reconnect_max_retries", 5)),
            lo=0,
        ),
        reconnect_backoff_s=_env_float(
            "FUBON_RECONNECT_BACKOFF_S",
            float(fubon_cfg.get("reconnect_backoff_s", 2.0)),
        ),
    )
