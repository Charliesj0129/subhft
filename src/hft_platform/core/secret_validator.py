"""Startup validation for required secrets and environment variables."""

from __future__ import annotations

import os

from structlog import get_logger

logger = get_logger("secret_validator")

# Required secrets that must be non-empty at startup
_REQUIRED_SECRETS = {
    "SHIOAJI_API_KEY": "Shioaji broker API key",
    "SHIOAJI_SECRET_KEY": "Shioaji broker secret key",
}

# Required infrastructure secrets (for Docker services)
_INFRA_SECRETS = {
    "CLICKHOUSE_PASSWORD": "ClickHouse database password",
    "REDIS_PASSWORD": "Redis password",
}

# Values that indicate a placeholder was not replaced.
# All entries are lowercase because comparison uses ``value.lower()``.
_PLACEHOLDER_VALUES = {"changeme", "your_key", "your_api_key", "password", "secret", ""}


def validate_secrets(*, require_broker: bool = True, require_infra: bool = False) -> list[str]:
    """Validate required secrets are present and not placeholders.

    Args:
        require_broker: If True, validate broker API credentials.
        require_infra: If True, validate infrastructure credentials.

    Returns:
        List of error messages. Empty list means all checks passed.
    """
    errors: list[str] = []

    secrets_to_check: dict[str, str] = {}
    if require_broker:
        # Check which broker is active
        broker = os.getenv("HFT_BROKER", "shioaji")
        if broker == "shioaji":
            secrets_to_check.update(_REQUIRED_SECRETS)
        elif broker == "fubon":
            secrets_to_check["HFT_FUBON_API_KEY"] = "Fubon broker API key"
            secrets_to_check["HFT_FUBON_PASSWORD"] = "Fubon broker password"

    if require_infra:
        secrets_to_check.update(_INFRA_SECRETS)

    for var_name, description in secrets_to_check.items():
        value = os.getenv(var_name, "")
        if not value:
            errors.append(f"Missing required secret: {var_name} ({description})")
        elif value.lower() in _PLACEHOLDER_VALUES:
            errors.append(f"Placeholder value detected for {var_name} ({description})")

    if errors:
        for err in errors:
            logger.error("Secret validation failed", error=err)
    else:
        logger.info("Secret validation passed", secrets_checked=len(secrets_to_check))

    return errors


class SecretValidationError(RuntimeError):
    """Raised when secret validation fails in live mode."""

    __slots__ = ("errors",)

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(f"Secret validation failed: {'; '.join(errors)}")


def validate_secrets_for_mode(
    *,
    mode: str | None = None,
    require_broker: bool = True,
    require_infra: bool = False,
) -> list[str]:
    """Mode-aware secret validation for bootstrap startup.

    In ``live`` mode, raises :class:`SecretValidationError` on any failure
    to enforce fail-fast behavior.  In all other modes (``sim``, ``replay``,
    etc.) failures are logged as warnings and the error list is returned
    without raising.

    Args:
        mode: Runtime mode override.  Falls back to ``HFT_MODE`` env var,
              defaulting to ``"sim"``.
        require_broker: Forward to :func:`validate_secrets`.
        require_infra: Forward to :func:`validate_secrets`.

    Returns:
        List of error messages (empty when all checks pass).

    Raises:
        SecretValidationError: When *mode* is ``"live"`` and validation fails.
    """
    if mode is None:
        mode = os.getenv("HFT_MODE", "sim").strip().lower()

    errors = validate_secrets(require_broker=require_broker, require_infra=require_infra)

    if not errors:
        logger.info("secret_validation_passed", mode=mode)
        return errors

    if mode == "live":
        logger.critical(
            "secret_validation_failed_live_mode",
            mode=mode,
            error_count=len(errors),
        )
        raise SecretValidationError(errors)

    # Non-live modes: warn but allow startup to continue
    for err in errors:
        logger.warning("secret_validation_warning", mode=mode, issue=err)

    return errors
