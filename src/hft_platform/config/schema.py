"""Config schema validation using msgspec.Struct.

Validates the merged config dict at startup. Fail-fast on invalid config
with SystemExit(1). Bypass with ``--skip-config-validation`` CLI flag or
``HFT_SKIP_CONFIG_VALIDATION=1`` env var.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

import msgspec
from structlog import get_logger

logger = get_logger("config.schema")


# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------


class StrategyConfig(msgspec.Struct, frozen=True):
    """Strategy section of the config."""

    id: str
    module: str

    # ``class`` is a Python keyword; msgspec renames via ``rename``.
    class_name: str = msgspec.field(name="class", default="")
    params: Dict[str, Any] = msgspec.field(default_factory=dict)


class PathsConfig(msgspec.Struct, frozen=True):
    """Paths section — all fields optional for backward compat."""

    symbols: Optional[str] = None
    strategy_limits: Optional[str] = None
    order_adapter: Optional[str] = None


class ReplayConfig(msgspec.Struct, frozen=True):
    """Replay section."""

    start_date: Optional[str] = None
    end_date: Optional[str] = None


class IntraDayPnlConfig(msgspec.Struct, frozen=True):
    """Intraday PnL limits."""

    soft_limit_ntd: int = 500
    hard_limit_ntd: int = 1000
    peak_drawdown_pct: float = 0.40
    soft_recovery_ntd: int = 300
    drawdown_recovery_pct: float = 0.20
    soft_limit_cooldown_s: int = 60
    peak_drawdown_min_peak_ntd: int = 200


class HftConfig(msgspec.Struct, frozen=True):
    """Top-level config schema.

    Fields with defaults are optional so that minimal configs (e.g. test
    fixtures) still pass validation.
    """

    mode: str = "sim"
    symbols: List[str] = msgspec.field(default_factory=lambda: ["2330"])
    broker: str = "shioaji"
    strategy: Optional[StrategyConfig] = None
    paths: Optional[PathsConfig] = None
    replay: Optional[ReplayConfig] = None
    prometheus_port: int = 9090

    intraday_pnl: Optional[IntraDayPnlConfig] = None

    # Allow extra keys that overlay YAMLs or settings.py may inject.
    # We capture them explicitly so msgspec doesn't reject unknown fields.
    env: Optional[str] = None

    # Loop-mode binding (loop_v1). When set, the loader resolves
    # config/loops/<loop_id>.yaml and forces strategy/broker from that
    # file. Strict schema is also enforced whenever loop_id is set, so
    # typos surface fast.
    loop_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_VALID_MODES = {"sim", "live", "replay"}
_VALID_BROKERS = {"shioaji", "fubon"}


class ConfigValidationError(Exception):
    """Raised when config validation fails."""


def _semantic_checks(cfg: HftConfig) -> List[str]:
    """Return a list of human-readable error strings (empty == OK)."""
    errors: List[str] = []

    if cfg.mode not in _VALID_MODES:
        errors.append(f"mode must be one of {_VALID_MODES}, got {cfg.mode!r}")

    if cfg.broker not in _VALID_BROKERS:
        errors.append(f"broker must be one of {_VALID_BROKERS}, got {cfg.broker!r}")

    if not cfg.symbols:
        errors.append("symbols list must not be empty")

    if not all(isinstance(s, str) and s for s in cfg.symbols):
        errors.append("every symbol must be a non-empty string")

    if cfg.prometheus_port < 1 or cfg.prometheus_port > 65535:
        errors.append(f"prometheus_port must be 1..65535, got {cfg.prometheus_port}")

    if cfg.strategy is not None:
        if not cfg.strategy.id:
            errors.append("strategy.id must be a non-empty string")
        if not cfg.strategy.module:
            errors.append("strategy.module must be a non-empty string")

    # Intraday PnL sanity
    if cfg.intraday_pnl is not None:
        pnl = cfg.intraday_pnl
        if pnl.soft_limit_ntd < 0:
            errors.append(f"intraday_pnl.soft_limit_ntd must be non-negative, got {pnl.soft_limit_ntd}")
        if pnl.hard_limit_ntd < 0:
            errors.append(f"intraday_pnl.hard_limit_ntd must be non-negative, got {pnl.hard_limit_ntd}")
        if pnl.soft_limit_ntd > pnl.hard_limit_ntd:
            errors.append(
                f"intraday_pnl.soft_limit_ntd ({pnl.soft_limit_ntd}) exceeds hard_limit_ntd ({pnl.hard_limit_ntd})"
            )

    return errors


def validate_config(config_dict: Dict[str, Any], *, strict: bool = False) -> HftConfig:
    """Validate *config_dict* against :class:`HftConfig`.

    Parameters
    ----------
    config_dict
        Merged config (base + overlays + settings.py + env vars + CLI).
    strict
        When True, unknown top-level keys cause :class:`ConfigValidationError`
        rather than silent stripping. Auto-enabled by the loader whenever
        ``loop_id`` is present or ``HFT_CONFIG_STRICT=1``.

    Returns the validated :class:`HftConfig` instance on success.

    Raises
    ------
    ConfigValidationError
        If structural or semantic validation fails, or (when ``strict``)
        unknown top-level keys are present.
    """
    # Strip unknown top-level keys that msgspec would reject, but log them.
    known_fields = {f.encode_name for f in msgspec.structs.fields(HftConfig)}
    extra_keys = set(config_dict.keys()) - known_fields
    clean = {k: v for k, v in config_dict.items() if k in known_fields}

    if extra_keys:
        if strict:
            raise ConfigValidationError(
                "unknown top-level keys (strict mode): " + ", ".join(sorted(extra_keys))
            )
        logger.debug("config_schema_extra_keys_ignored", keys=sorted(extra_keys))

    # Structural validation via msgspec.convert
    try:
        cfg = msgspec.convert(clean, HftConfig, strict=False)
    except msgspec.ValidationError as exc:
        raise ConfigValidationError(f"Config structure invalid: {exc}") from exc

    # Semantic validation
    errors = _semantic_checks(cfg)
    if errors:
        raise ConfigValidationError("Config semantic errors:\n  - " + "\n  - ".join(errors))

    return cfg


def validate_config_or_exit(
    config_dict: Dict[str, Any], *, strict: bool = False
) -> HftConfig | None:
    """Validate config; on failure log errors and ``sys.exit(1)``.

    If ``HFT_SKIP_CONFIG_VALIDATION=1`` is set, validation is skipped and
    ``None`` is returned.
    """
    if os.getenv("HFT_SKIP_CONFIG_VALIDATION", "0") == "1":
        logger.warning("config_validation_skipped")
        return None

    try:
        return validate_config(config_dict, strict=strict)
    except ConfigValidationError as exc:
        logger.error("config_validation_failed", error=str(exc), strict=strict)
        sys.exit(1)
