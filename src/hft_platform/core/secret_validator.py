"""Startup validation for required secrets and environment variables."""

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

# Values that indicate a placeholder was not replaced
_PLACEHOLDER_VALUES = {"changeme", "YOUR_KEY", "password", "secret", ""}


def validate_secrets(*, require_broker: bool = True, require_infra: bool = False) -> list[str]:
    """Validate required secrets are present and not placeholders.

    Args:
        require_broker: If True, validate broker API credentials.
        require_infra: If True, validate infrastructure credentials.

    Returns:
        List of error messages. Empty list means all checks passed.
    """
    errors: list[str] = []

    secrets_to_check = {}
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
