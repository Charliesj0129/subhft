"""Shioaji client configuration — centralized env var parsing.

Extracts all environment variable reads from ``ShioajiClient.__init__``
into a frozen dataclass so that the config surface is explicit, testable,
and shareable across submodules without re-parsing.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from structlog import get_logger

logger = get_logger("feed_adapter.config")


def _as_bool(value: Any) -> bool:
    """Parse truthy string values consistently."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _env_bool(key: str, default: str = "0") -> bool:
    return _as_bool(os.getenv(key, default))


def _env_int(key: str, default: int, *, min_val: int = 0) -> int:
    try:
        return max(min_val, int(os.getenv(key, str(default))))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True, slots=True)
class ShioajiClientConfig:
    """Immutable configuration for ShioajiClient, parsed from env + dict."""

    # --- Subscription ---
    max_subscriptions: int = 200
    contracts_timeout: int = 10000
    fetch_contract: bool = True
    subscribe_trade: bool = True
    allow_symbol_fallback: bool = False
    allow_synthetic_contracts: bool = False
    index_exchange: str = "TSE"
    resubscribe_cooldown: float = 1.5
    resubscribe_delay_s: float = 0.5

    # --- CA / Certificate ---
    activate_ca: bool = False
    ca_path: str | None = None
    ca_password: str | None = None

    # --- Simulation ---
    simulation: bool = False

    # --- Quote dispatch ---
    quote_dispatch_async: bool = True
    quote_dispatch_queue_size: int = 8192
    quote_dispatch_batch_max: int = 32
    quote_dispatch_metrics_every: int = 128

    # --- Reconnect / Login ---
    reconnect_backoff_s: float = 30.0
    reconnect_backoff_max_s: float = 600.0
    login_timeout_s: float = 20.0
    reconnect_timeout_s: float = 45.0
    reconnect_subscribe_timeout_s: float = 30.0
    login_retry_max: int = 1

    # --- API cache ---
    api_cache_max_size: int = 1000
    positions_cache_ttl_s: float = 1.5
    usage_cache_ttl_s: float = 5.0
    account_cache_ttl_s: float = 5.0
    margin_cache_ttl_s: float = 5.0
    profit_cache_ttl_s: float = 10.0
    positions_detail_cache_ttl_s: float = 10.0

    # --- Quote version ---
    quote_version_mode: str = "auto"
    quote_version_strict: bool = False
    quote_version: str = "v1"

    # --- Quote schema guard ---
    quote_schema_guard: bool = True
    quote_schema_guard_strict: bool = True
    quote_schema_mismatch_log_every: int = 100

    # --- Quote watchdog ---
    quote_watchdog_interval_s: float = 5.0
    quote_no_data_s: float = 30.0
    quote_watchdog_skip_off_hours: bool = True
    quote_off_hours_log_interval_s: float = 60.0
    quote_pending_stall_warn_s: float = 120.0

    # --- Quote flap ---
    quote_force_relogin_s: float = 15.0
    quote_flap_window_s: float = 60.0
    quote_flap_threshold: int = 5
    quote_flap_cooldown_s: float = 300.0

    # --- Quote event retry ---
    quote_event_retry_s: float = 5.0

    # --- Rate limiter ---
    api_soft_cap: int = 20
    api_hard_cap: int = 25
    api_window_s: int = 5

    # --- Session refresh ---
    session_refresh_interval_s: float = 86400.0
    session_refresh_check_interval_s: float = 3600.0
    session_refresh_holiday_aware: bool = True
    session_refresh_verify_timeout_s: float = 10.0

    # --- Market open ---
    market_open_grace_s: float = 60.0

    # --- Contract retry / refresh ---
    contract_retry_s: float = 60.0
    contract_refresh_s: float = 86400.0
    contract_cache_path: str = "config/contracts.json"
    contract_refresh_resubscribe_policy: str = "none"
    contract_refresh_status_path: str = "outputs/contract_refresh_status.json"

    # --- Session lock ---
    session_lock_enabled: bool = True
    session_lock_path: str = ".wal/.locks/shioaji_session_default.lock"

    # --- Config path ---
    config_path: str = "config/base/symbols.yaml"


def load_shioaji_config(
    settings: dict[str, Any] | None = None,
    *,
    config_path: str | None = None,
) -> ShioajiClientConfig:
    """Build ``ShioajiClientConfig`` from env vars + optional settings dict.

    Parameters
    ----------
    settings:
        The ``shioaji_config`` dict passed to ``ShioajiClient.__init__``.
    config_path:
        Explicit symbols config path. Resolved from env/defaults when *None*.
    """
    settings = settings or {}

    # --- CA ---
    if "activate_ca" in settings:
        activate_ca = _as_bool(settings.get("activate_ca"))
    else:
        activate_ca = _env_bool("SHIOAJI_ACTIVATE_CA") or _env_bool("HFT_ACTIVATE_CA")

    ca_path = settings.get("ca_path") or os.getenv("SHIOAJI_CA_PATH") or os.getenv("CA_CERT_PATH")
    ca_password = settings.get("ca_password") or os.getenv("SHIOAJI_CA_PASSWORD") or os.getenv("CA_PASSWORD")
    if not ca_password:
        env_key = settings.get("ca_password_env")
        if env_key:
            ca_password = os.getenv(str(env_key))

    # --- Simulation ---
    sim_override = settings.get("simulation") if "simulation" in settings else None
    if sim_override is None:
        is_sim = os.getenv("HFT_MODE", "real") == "sim"
    else:
        is_sim = _as_bool(sim_override)

    # Deactivate CA in simulation mode.
    if is_sim:
        activate_ca = False

    # --- Config path ---
    resolved_config_path = config_path
    if resolved_config_path is None:
        resolved_config_path = os.getenv("SYMBOLS_CONFIG")
        if not resolved_config_path:
            if os.path.exists("config/symbols.yaml"):
                resolved_config_path = "config/symbols.yaml"
            else:
                resolved_config_path = "config/base/symbols.yaml"

    # --- Quote version ---
    quote_version_mode = os.getenv("HFT_QUOTE_VERSION", "auto").strip().lower()
    if quote_version_mode not in {"v0", "v1", "auto"}:
        quote_version_mode = "auto"
    quote_version = "v1" if quote_version_mode in {"v1", "auto"} else "v0"

    # --- Session lock ---
    lock_id_raw = (
        os.getenv("SHIOAJI_ACCOUNT") or os.getenv("SHIOAJI_PERSON_ID") or os.getenv("SHIOAJI_API_KEY") or "default"
    )
    lock_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(lock_id_raw).strip())[:64] or "default"
    lock_dir = os.getenv("HFT_SHIOAJI_SESSION_LOCK_DIR", ".wal/.locks")
    session_lock_path = str(Path(lock_dir) / f"shioaji_session_{lock_id}.lock")

    # --- Contract refresh status ---
    contract_refresh_status_path = os.getenv(
        "HFT_CONTRACT_REFRESH_STATUS_PATH", "outputs/contract_refresh_status.json"
    )
    contract_refresh_resubscribe_policy = (
        os.getenv("HFT_CONTRACT_REFRESH_RESUBSCRIBE_POLICY", "none").strip().lower() or "none"
    )

    cfg = ShioajiClientConfig(
        max_subscriptions=200,
        contracts_timeout=_env_int("SHIOAJI_CONTRACTS_TIMEOUT", 10000),
        fetch_contract=os.getenv("SHIOAJI_FETCH_CONTRACT", "1") != "0",
        subscribe_trade=os.getenv("SHIOAJI_SUBSCRIBE_TRADE", "1") != "0",
        allow_symbol_fallback=_env_bool("HFT_ALLOW_SYMBOL_FALLBACK"),
        allow_synthetic_contracts=_env_bool("HFT_ALLOW_SYNTHETIC_CONTRACTS"),
        index_exchange=os.getenv("HFT_INDEX_EXCHANGE", "TSE").upper(),
        resubscribe_cooldown=_env_float("HFT_RESUBSCRIBE_COOLDOWN", 1.5),
        resubscribe_delay_s=_env_float("HFT_RESUBSCRIBE_DELAY_S", 0.5),
        activate_ca=activate_ca,
        ca_path=ca_path,
        ca_password=ca_password,
        simulation=is_sim,
        quote_dispatch_async=_env_bool("HFT_SHIOAJI_QUOTE_DISPATCH_THREAD", "1"),
        quote_dispatch_queue_size=_env_int("HFT_SHIOAJI_QUOTE_CB_QUEUE_SIZE", 8192, min_val=1),
        quote_dispatch_batch_max=_env_int("HFT_SHIOAJI_QUOTE_CB_BATCH_MAX", 32, min_val=1),
        quote_dispatch_metrics_every=_env_int("HFT_SHIOAJI_QUOTE_CB_METRICS_EVERY", 128, min_val=1),
        reconnect_backoff_s=_env_float("HFT_RECONNECT_BACKOFF_S", 30.0),
        reconnect_backoff_max_s=_env_float("HFT_RECONNECT_BACKOFF_MAX_S", 600.0),
        login_timeout_s=_env_float("HFT_SHIOAJI_LOGIN_TIMEOUT_S", 20.0),
        reconnect_timeout_s=_env_float("HFT_SHIOAJI_RECONNECT_TIMEOUT_S", 45.0),
        reconnect_subscribe_timeout_s=_env_float("HFT_SHIOAJI_RECONNECT_SUBSCRIBE_TIMEOUT_S", 30.0),
        login_retry_max=_env_int("HFT_SHIOAJI_LOGIN_RETRY_MAX", 1, min_val=0),
        api_cache_max_size=_env_int("HFT_API_CACHE_MAX_SIZE", 1000),
        positions_cache_ttl_s=_env_float("HFT_POSITIONS_CACHE_TTL_S", 1.5),
        usage_cache_ttl_s=_env_float("HFT_USAGE_CACHE_TTL_S", 5.0),
        account_cache_ttl_s=_env_float("HFT_ACCOUNT_CACHE_TTL_S", 5.0),
        margin_cache_ttl_s=_env_float("HFT_MARGIN_CACHE_TTL_S", 5.0),
        profit_cache_ttl_s=_env_float("HFT_PROFIT_CACHE_TTL_S", 10.0),
        positions_detail_cache_ttl_s=_env_float("HFT_POSITION_DETAIL_CACHE_TTL_S", 10.0),
        quote_version_mode=quote_version_mode,
        quote_version_strict=_env_bool("HFT_QUOTE_VERSION_STRICT"),
        quote_version=quote_version,
        quote_schema_guard=_env_bool("HFT_QUOTE_SCHEMA_GUARD", "1"),
        quote_schema_guard_strict=_env_bool("HFT_QUOTE_SCHEMA_GUARD_STRICT", "1"),
        quote_schema_mismatch_log_every=_env_int("HFT_QUOTE_SCHEMA_MISMATCH_LOG_EVERY", 100, min_val=1),
        quote_watchdog_interval_s=_env_float("HFT_QUOTE_WATCHDOG_S", 5.0),
        quote_no_data_s=_env_float("HFT_QUOTE_NO_DATA_S", 30.0),
        quote_watchdog_skip_off_hours=_as_bool(os.getenv("HFT_QUOTE_WATCHDOG_SKIP_OFF_HOURS", "1")),
        quote_off_hours_log_interval_s=_env_float("HFT_QUOTE_OFF_HOURS_LOG_INTERVAL_S", 60.0),
        quote_pending_stall_warn_s=_env_float("HFT_QUOTE_PENDING_STALL_WARN_S", 120.0),
        quote_force_relogin_s=_env_float("HFT_QUOTE_FORCE_RELOGIN_S", 15.0),
        quote_flap_window_s=_env_float("HFT_QUOTE_FLAP_WINDOW_S", 60.0),
        quote_flap_threshold=_env_int("HFT_QUOTE_FLAP_THRESHOLD", 5),
        quote_flap_cooldown_s=_env_float("HFT_QUOTE_FLAP_COOLDOWN_S", 300.0),
        quote_event_retry_s=_env_float("HFT_QUOTE_EVENT_RETRY_S", 5.0),
        api_soft_cap=_env_int("HFT_SHIOAJI_API_SOFT_CAP", 20),
        api_hard_cap=_env_int("HFT_SHIOAJI_API_HARD_CAP", 25),
        api_window_s=_env_int("HFT_SHIOAJI_API_WINDOW_S", 5),
        session_refresh_interval_s=_env_float("HFT_SESSION_REFRESH_S", 86400.0),
        session_refresh_holiday_aware=_env_bool("HFT_SESSION_REFRESH_HOLIDAY_AWARE", "1"),
        session_refresh_verify_timeout_s=_env_float("HFT_SESSION_REFRESH_VERIFY_TIMEOUT_S", 10.0),
        market_open_grace_s=_env_float("HFT_MARKET_OPEN_GRACE_S", 60.0),
        contract_retry_s=_env_float("HFT_CONTRACT_RETRY_S", 60.0),
        contract_refresh_s=_env_float("HFT_CONTRACT_REFRESH_S", 86400.0),
        contract_cache_path=os.getenv("HFT_CONTRACT_CACHE_PATH", "config/contracts.json"),
        contract_refresh_resubscribe_policy=contract_refresh_resubscribe_policy,
        contract_refresh_status_path=contract_refresh_status_path,
        session_lock_enabled=_as_bool(os.getenv("HFT_SHIOAJI_SESSION_LOCK_ENABLED", "1")),
        session_lock_path=session_lock_path,
        config_path=resolved_config_path,
    )

    logger.debug("shioaji_config_loaded", simulation=cfg.simulation, quote_version=cfg.quote_version)
    return cfg
