"""Fubon broker configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass

from structlog import get_logger

logger = get_logger("fubon.config")


@dataclass(frozen=True, slots=True)
class FubonClientConfig:
    """Immutable configuration for Fubon SDK connection."""

    user_id: str = ""
    password: str = ""
    cert_path: str = ""
    cert_password: str = ""
    simulation: bool = True
    reconnect_max_retries: int = 5
    reconnect_backoff_s: float = 2.0


def load_fubon_config(settings: dict | None = None) -> FubonClientConfig:
    """Build config from settings dict + env vars (env wins)."""
    s = settings or {}
    fc = s.get("fubon", {})
    return FubonClientConfig(
        user_id=os.getenv("FUBON_ID", fc.get("user_id", "")),
        password=os.getenv("FUBON_PASSWORD", fc.get("password", "")),
        cert_path=os.getenv("FUBON_CERT_PATH", fc.get("cert_path", "")),
        cert_password=os.getenv("FUBON_CERT_PASSWORD", fc.get("cert_password", "")),
        simulation=os.getenv("FUBON_SIMULATION", str(fc.get("simulation", True))).lower() in ("1", "true", "yes"),
    )
