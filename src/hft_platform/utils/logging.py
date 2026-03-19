import logging
import sys
from typing import Any

import structlog

_SENSITIVE_PATTERNS: frozenset[str] = frozenset(
    {
        "api_key",
        "secret_key",
        "password",
        "token",
        "cert_path",
        "secret",
        "credential",
        "authorization",
    }
)
_MASK = "***"


def credential_scrubber(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Structlog processor that masks sensitive field values."""
    for key in event_dict:
        if any(p in key.lower() for p in _SENSITIVE_PATTERNS):
            event_dict[key] = _MASK
    return event_dict


def configure_logging(level: int = logging.INFO) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            credential_scrubber,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)


def get_logger(name: str):
    return structlog.get_logger(name)
