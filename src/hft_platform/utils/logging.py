import logging
import re
import sys
from typing import Any, MutableMapping

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
_JWT_MASK = "***JWT***"

# Bug #31: JWT (header.payload.signature, base64url) and Bearer tokens leak via
# `error=str(exc)` from broker SDK exceptions. Key-name scrubbing alone misses
# them because the leaky key is "error". Scrub VALUES too.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)\S+")


def _scrub_value_str(v: str) -> str:
    if "eyJ" in v:
        v = _JWT_RE.sub(_JWT_MASK, v)
    if "earer" in v:
        v = _BEARER_RE.sub(r"\1***", v)
    return v


def credential_scrubber(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Structlog processor that masks sensitive field values."""
    for key in list(event_dict):
        if any(p in key.lower() for p in _SENSITIVE_PATTERNS):
            event_dict[key] = _MASK
            continue
        v = event_dict[key]
        if isinstance(v, str):
            event_dict[key] = _scrub_value_str(v)
    return event_dict


def configure_logging(level: int = logging.INFO) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            credential_scrubber,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)


def get_logger(name: str):
    return structlog.get_logger(name)
