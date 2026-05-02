"""Shared ClickHouse client factory.

Provides a single, canonical source of truth for resolving ClickHouse
connection parameters from the environment and creating clients.

Usage::

    from hft_platform.infra.ch_client import get_ch_client, get_ch_config

    # Create a ready-to-use client:
    client = get_ch_client()

    # Or just resolve config (e.g. to pass to a custom client constructor):
    cfg = get_ch_config()

Environment variable precedence (username):

1. ``HFT_CLICKHOUSE_USER``              (canonical)
2. ``HFT_CLICKHOUSE_USERNAME``          (deprecated — logs structlog warning)
3. ``CLICKHOUSE_USER``                  (generic fallback)
4. ``CLICKHOUSE_USERNAME``              (deprecated — logs structlog warning)
5. ``"default"``                        (hard-coded last-resort)

Environment variable precedence (password):

1. ``HFT_CLICKHOUSE_PASSWORD``          (canonical)
2. ``CLICKHOUSE_PASSWORD``              (generic fallback)
3. ``""``                               (empty string last-resort)

Other environment variables:

- ``HFT_CLICKHOUSE_HOST``     — default ``"localhost"``
- ``HFT_CLICKHOUSE_PORT``     — default ``8123``, parsed to ``int``
- ``HFT_CLICKHOUSE_DATABASE`` — default ``"hft"``

No connection is established on import.  Both functions are side-effect-free
apart from environment reads and (for ``get_ch_client``) the network call made
by ``clickhouse_connect.get_client()``.
"""

from __future__ import annotations

import os
import warnings
from typing import Any

from structlog import get_logger

logger = get_logger("infra.ch_client")

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_DEPRECATED_HFT_USERNAME = "HFT_CLICKHOUSE_USERNAME"
_DEPRECATED_PLAIN_USERNAME = "CLICKHOUSE_USERNAME"

_TODO_DEPRECATION_REMOVE = "TODO(2026-Q3): remove deprecated env var fallbacks — deprecated since 2026-03"


def _resolve_username() -> str:
    """Resolve ClickHouse username with deprecation fallback chain."""
    # 1. Canonical
    value = os.getenv("HFT_CLICKHOUSE_USER")
    if value:
        return value

    # 2. Deprecated HFT-prefixed alias
    value = os.getenv(_DEPRECATED_HFT_USERNAME)
    if value:
        warnings.warn(
            f"{_DEPRECATED_HFT_USERNAME} is deprecated, use HFT_CLICKHOUSE_USER instead",
            DeprecationWarning,
            stacklevel=3,
        )
        logger.warning(
            "Deprecated env var used; migrate to HFT_CLICKHOUSE_USER",
            deprecated_var=_DEPRECATED_HFT_USERNAME,
            # _TODO_DEPRECATION_REMOVE
        )
        return value

    # 3. Generic fallback (no deprecation warning — still reasonably common)
    value = os.getenv("CLICKHOUSE_USER")
    if value:
        return value

    # 4. Deprecated generic alias
    value = os.getenv(_DEPRECATED_PLAIN_USERNAME)
    if value:
        warnings.warn(
            f"{_DEPRECATED_PLAIN_USERNAME} is deprecated, use HFT_CLICKHOUSE_USER instead",
            DeprecationWarning,
            stacklevel=3,
        )
        logger.warning(
            "Deprecated env var used; migrate to HFT_CLICKHOUSE_USER",
            deprecated_var=_DEPRECATED_PLAIN_USERNAME,
            # _TODO_DEPRECATION_REMOVE
        )
        return value

    return "default"


def _resolve_password() -> str:
    """Resolve ClickHouse password with fallback chain."""
    return os.getenv("HFT_CLICKHOUSE_PASSWORD") or os.getenv("CLICKHOUSE_PASSWORD") or ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_ch_config() -> dict[str, Any]:
    """Return the resolved ClickHouse connection config as a plain dict.

    Useful for modules that need the parameters but construct their own
    client (e.g. with extra options such as ``compress`` or ``interface``).

    Returns a dict with keys: ``host``, ``port`` (int), ``username``,
    ``password``, ``database``.
    """
    return {
        "host": os.getenv("HFT_CLICKHOUSE_HOST", "localhost"),
        "port": int(os.getenv("HFT_CLICKHOUSE_PORT", "8123")),
        "username": _resolve_username(),
        "password": _resolve_password(),
        "database": os.getenv("HFT_CLICKHOUSE_DATABASE", "hft"),
    }


def get_ch_client(**kwargs: Any) -> Any:
    """Return a connected ``clickhouse_connect`` client.

    Connection parameters are read from the environment (see module docstring).
    Any keyword argument passed here overrides the corresponding env-derived
    value, allowing callers to inject e.g. ``compress=True`` or
    ``interface="native"`` without reimplementing the env lookup chain.

    Raises
    ------
    RuntimeError
        If ``clickhouse_connect`` is not installed.
    """
    try:
        import clickhouse_connect
    except ImportError as exc:
        raise RuntimeError("clickhouse_connect is not installed — run: pip install clickhouse-connect") from exc

    cfg = get_ch_config()
    # Caller overrides win; pop database if not supported by the client call
    merged = {**cfg, **kwargs}
    return clickhouse_connect.get_client(**merged)
