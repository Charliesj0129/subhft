"""Observability for legacy Shioaji Solace reconnect parameters.

shioaji 1.3.3 delegates its messaging-layer reconnect to pysolace/libsolace,
governed entirely by five env vars read at *import time* in
``shioaji/config.py``:

    SOL_CONNECT_TIMEOUT_MS   (default 3000)  connect attempt timeout
    SOL_RECONNECT_RETRIES    (default 10)     reconnect attempts before down
    SOL_RECONNECT_RETRY_WAIT (default 3000)   ms between reconnect attempts
    SOL_KEEP_ALIVE_MS        (default 3000)   keep-alive interval
    SOL_KEEP_ALIVE_LIMIT     (default 3)      missed keep-alives before drop

Shioaji 1.5.x moved messaging into its native core and removed
``shioaji.config``. In that case this module returns ``None`` because the
legacy values are no longer observable through Python. The platform leaves
these at SDK defaults and never logged them, so the
2026-06-15 18h spin-hang postmortem had no record of the SDK's reconnect
cadence. This module surfaces the *effective* values once at startup. It is
pure observability — it sets nothing and never raises.
"""

from __future__ import annotations

import importlib
import threading
from typing import Any

from structlog import get_logger

logger = get_logger("feed_adapter.solace_env")

# Attribute names on ``shioaji.config`` that govern Solace reconnect.
SOLACE_RECONNECT_PARAMS: tuple[str, ...] = (
    "SOL_CONNECT_TIMEOUT_MS",
    "SOL_RECONNECT_RETRIES",
    "SOL_RECONNECT_RETRY_WAIT",
    "SOL_KEEP_ALIVE_MS",
    "SOL_KEEP_ALIVE_LIMIT",
)

_log_lock = threading.Lock()
_logged = False


def read_solace_reconnect_params() -> dict[str, Any] | None:
    """Return the effective Shioaji Solace reconnect params.

    Returns ``None`` if ``shioaji.config`` cannot be imported (e.g. Shioaji
    1.5.x uses its native messaging core, or the SDK is not installed).
    Missing attributes map to ``None``.
    """
    try:
        mod = importlib.import_module("shioaji.config")
    except Exception:
        return None
    return {name: getattr(mod, name, None) for name in SOLACE_RECONNECT_PARAMS}


def log_solace_reconnect_params(*, force: bool = False) -> dict[str, Any] | None:
    """Log the effective Solace reconnect params once per process.

    Best-effort: returns the params dict that was logged, or ``None`` if the
    SDK config is unavailable or the params were already logged (and not
    ``force``-d). Never raises.
    """
    global _logged
    with _log_lock:
        if _logged and not force:
            return None
        params = read_solace_reconnect_params()
        if params is None:
            return None
        _logged = True
    logger.info(
        "shioaji_solace_reconnect_params",
        **{name.lower(): value for name, value in params.items()},
    )
    return params


def reset_solace_reconnect_log() -> None:
    """Test/diagnostic helper: allow the params to be logged again."""
    global _logged
    with _log_lock:
        _logged = False
