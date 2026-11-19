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
        "bot_token",
        "person_id",
        "passwd",
        "pwd",
        "dsn",
        "private_key",
        "cookie",
    }
)
_MASK = "***REDACTED***"
_JWT_MASK = "***JWT***"

_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)\S+")
_TELEGRAM_BOT_TOKEN_RE = re.compile(r"(?<!\d)\d{8,11}:[A-Za-z0-9_\-]{30,}")
_TELEGRAM_TOKEN_MASK = "***TELEGRAM_TOKEN***"

_SECRET_NAME = (
    r"api[_-]?key|secret[_-]?key|private[_-]?key|passw(?:or)?d|pwd|secret|token|authorization|person[_-]?id|cookie|dsn"
)
_KV_SECRET_RE = re.compile(r"(?i)((?:" + _SECRET_NAME + r")[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s\"',;&)}\]]+)")
_NATIONAL_ID_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][12]\d{8}(?![A-Za-z0-9])")
_NATIONAL_ID_MASK = "***ID***"


def _mask_kv_value(m: "re.Match[str]") -> str:
    if m.group(2).startswith("***"):
        return m.group(0)
    return m.group(1) + _MASK


def _scrub_value_str(v: str) -> str:
    if "eyJ" in v:
        v = _JWT_RE.sub(_JWT_MASK, v)
    if "earer" in v:
        v = _BEARER_RE.sub(r"\1***", v)
    if ":" in v:
        v = _TELEGRAM_BOT_TOKEN_RE.sub(_TELEGRAM_TOKEN_MASK, v)
    if ":" in v or "=" in v:
        v = _KV_SECRET_RE.sub(_mask_kv_value, v)
    return _NATIONAL_ID_RE.sub(_NATIONAL_ID_MASK, v)


def _scrub_mapping(d: MutableMapping[Any, Any]) -> MutableMapping[Any, Any]:
    """Apply key-name + value-string scrubbing to a single mapping level,
    recursing into nested dict / list values. Non-str keys (e.g. int, tuple
    in metric labels) are skipped for the key-pattern check but their values
    are still recursed into."""
    for key in list(d):
        if isinstance(key, str) and any(p in key.lower() for p in _SENSITIVE_PATTERNS):
            d[key] = _MASK
            continue
        v = d[key]
        if isinstance(v, str):
            d[key] = _scrub_value_str(v)
        elif isinstance(v, BaseException):
            d[key] = _scrub_value_str(repr(v))
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


class _CurrentStderr:
    """A file object that resolves ``sys.stderr`` on every write.

    The stream cannot be captured once and reused. Passing ``file=sys.stderr``
    to ``PrintLoggerFactory`` binds it at configure time, and binding it when
    the logger is built is no better: structlog caches bound loggers
    (``cache_logger_on_first_use``), so whichever ``sys.stderr`` was installed
    at first use gets pinned for the rest of the process -- a test's temporary
    ``redirect_stderr`` buffer, for instance. Delegating per write keeps the
    sink correct however late the swap happens, which is the property
    structlog's own stdout default had.
    """

    def write(self, data: str) -> int:
        return sys.stderr.write(data)

    def flush(self) -> None:
        sys.stderr.flush()


_CURRENT_STDERR = _CurrentStderr()


def _stderr_logger_factory(*_args: Any) -> Any:
    return structlog.PrintLogger(file=_CURRENT_STDERR)


def _scrubbing_record_factory_for(previous: Any) -> Any:
    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return record
        scrubbed = _scrub_value_str(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = None
        if record.exc_info and record.exc_info[0] is not None and not record.exc_text:
            record.exc_text = _scrub_value_str(logging.Formatter().formatException(record.exc_info))
        if record.stack_info:
            record.stack_info = _scrub_value_str(record.stack_info)
        return record

    factory._hft_scrubbing = True  # type: ignore[attr-defined]  # marker so install is idempotent
    return factory


def install_stdlib_scrubber() -> None:
    """Scrub every stdlib ``logging`` record at creation time.

    Third-party libraries (asyncio's "Task exception was never retrieved", httpx,
    python-telegram-bot, ...) log through stdlib ``logging`` straight to the root
    handler and never enter the structlog processor chain, so ``credential_scrubber``
    cannot see them. A record factory runs for every record whatever handler later
    emits it, including handlers added after this call. Idempotent.
    """
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_hft_scrubbing", False):
        return
    logging.setLogRecordFactory(_scrubbing_record_factory_for(previous))


def configure_logging(level: int = logging.INFO) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            credential_scrubber,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=_stderr_logger_factory,
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)
    install_stdlib_scrubber()
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str):
    return structlog.get_logger(name)


structlog.configure(logger_factory=_stderr_logger_factory)
