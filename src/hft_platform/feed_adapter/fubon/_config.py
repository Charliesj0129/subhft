"""Fubon Neo client configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FubonClientConfig:
    """Minimal config for Fubon Neo SDK connection."""

    user_id: str = ""
    password: str = ""
    cert_path: str = ""
    cert_password: str = ""
    simulation: bool = True
    reconnect_max_retries: int = 5
    reconnect_backoff_s: float = 2.0
