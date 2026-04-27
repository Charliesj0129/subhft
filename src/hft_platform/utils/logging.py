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
        # P0-b (2026-04-27): a prior Infra investigator session leaked a live
        # Telegram bot token via the field name `telegram_token`. Add explicit
        # bot-token substring matcher so any field whose key (case-insensitive)
        # contains "bot_token" is masked before the JSON renderer serialises it.
        "bot_token",
    }
)
_MASK = "***REDACTED***"
_JWT_MASK = "***JWT***"

# Bug #31: JWT (header.payload.signature, base64url) and Bearer tokens leak via
# `error=str(exc)` from broker SDK exceptions. Key-name scrubbing alone misses
# them because the leaky key is "error". Scrub VALUES too.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)\S+")
# P0-b (2026-04-27): Telegram bot-token format is `<bot_id>:<token>` where
# bot_id is 8-11 digits and the token is 30+ url-safe-base64 chars. Scrub
# matching values regardless of key name (covers `event=...8794586948:AAFP...`
# style log messages where the token is embedded in a free-form string).
_TELEGRAM_BOT_TOKEN_RE = re.compile(r"\b\d{8,11}:[A-Za-z0-9_\-]{30,}\b")
_TELEGRAM_TOKEN_MASK = "***TELEGRAM_TOKEN***"


def _scrub_value_str(v: str) -> str:
    if "eyJ" in v:
        v = _JWT_RE.sub(_JWT_MASK, v)
    if "earer" in v:
        v = _BEARER_RE.sub(r"\1***", v)
    # Cheap pre-check: a Telegram bot token always contains `:`. Skip regex
    # for the common case (log messages with no colon).
    if ":" in v:
        v = _TELEGRAM_BOT_TOKEN_RE.sub(_TELEGRAM_TOKEN_MASK, v)
    return v


def _scrub_mapping(d: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Apply key-name + value-string scrubbing to a single mapping level,
    recursing into nested dict / list values."""
    for key in list(d):
        if any(p in key.lower() for p in _SENSITIVE_PATTERNS):
            d[key] = _MASK
            continue
        v = d[key]
        if isinstance(v, str):
            d[key] = _scrub_value_str(v)
        elif isinstance(v, dict):
            _scrub_mapping(v)
        elif isinstance(v, list):
            d[key] = _scrub_list(v)
    return d


def _scrub_list(items: list[Any]) -> list[Any]:
    """Walk a list, recursing into dict elements and scrubbing str elements
    via the value regex. Returns the same list (mutated in place where safe)."""
    for i, elem in enumerate(items):
        if isinstance(elem, dict):
            _scrub_mapping(elem)
        elif isinstance(elem, str):
            items[i] = _scrub_value_str(elem)
        elif isinstance(elem, list):
            items[i] = _scrub_list(elem)
    return items


def credential_scrubber(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Structlog processor that masks sensitive field values.

    Recurses into nested dict / list values so a token nested inside a
    `payload={...}` or `items=[{...}, ...]` structure is also redacted.
    """
    return _scrub_mapping(event_dict)


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
