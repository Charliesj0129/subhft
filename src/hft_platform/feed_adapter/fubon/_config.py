"""Fubon broker configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FubonClientConfig:
    """Configuration for Fubon Neo SDK client."""

    user_id: str = ""
    password: str = ""
    cert_path: str = ""
    cert_password: str = ""
    simulation: bool = True
    reconnect_max_retries: int = 5
    reconnect_backoff_s: float = 2.0


def load_fubon_config(settings: dict | None = None) -> FubonClientConfig:
    """Build a frozen config from env vars + optional dict overlay."""
    s = settings or {}
    fubon_cfg = s.get("fubon", {})
    return FubonClientConfig(
        user_id=os.getenv("FUBON_ID", fubon_cfg.get("user_id", "")),
        password=os.getenv("FUBON_PASSWORD", fubon_cfg.get("password", "")),
        cert_path=os.getenv("FUBON_CERT_PATH", fubon_cfg.get("cert_path", "")),
        cert_password=os.getenv("FUBON_CERT_PASSWORD", fubon_cfg.get("cert_password", "")),
        simulation=os.getenv("FUBON_SIMULATION", str(fubon_cfg.get("simulation", True))).lower()
        in ("1", "true", "yes"),
        reconnect_max_retries=int(
            os.getenv(
                "FUBON_RECONNECT_MAX_RETRIES",
                str(fubon_cfg.get("reconnect_max_retries", 5)),
            )
        ),
        reconnect_backoff_s=float(
            os.getenv(
                "FUBON_RECONNECT_BACKOFF_S",
                str(fubon_cfg.get("reconnect_backoff_s", 2.0)),
            )
        ),
    )
