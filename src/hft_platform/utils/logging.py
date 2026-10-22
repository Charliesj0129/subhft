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
        # 2026-10-03 audit: the Shioaji login identity and CA password reach logs
        # under these names (the SDK's `person_id`, `ca_passwd`), none of which the
        # list above contained ("passwd" is not a substring of "password").
        "person_id",
        "passwd",
        "pwd",
        "dsn",  # connection strings embed the password
        "private_key",
        "cookie",
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
# The token is also embedded in the API URL as `https://api.telegram.org/bot<id>:<tok>/...`
# (python-telegram-bot logs it at INFO). A leading `\b` cannot match between the `t` of
# `bot` and the first digit (both are word characters), so that form was NOT masked;
# a "no digit before" lookbehind matches it and still refuses to start mid-number.
_TELEGRAM_BOT_TOKEN_RE = re.compile(r"(?<!\d)\d{8,11}:[A-Za-z0-9_\-]{30,}")
_TELEGRAM_TOKEN_MASK = "***TELEGRAM_TOKEN***"

# `name=value` / `name: value` / `"name": "value"` inside free text: exception
# messages and third-party log lines carry secrets this way and have no key for the
# name-based scrub to look at. The name must be IMMEDIATELY followed by the separator,
# so `TokenExpiredError: ...` and `max_tokens=5` are left alone (diagnostics stay readable).
_SECRET_NAME = (
    r"api[_-]?key|secret[_-]?key|private[_-]?key|passw(?:or)?d|pwd|secret|token|authorization|person[_-]?id|cookie|dsn"
)
_KV_SECRET_RE = re.compile(r"(?i)((?:" + _SECRET_NAME + r")[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s\"',;&)}\]]+)")
# Taiwan national ID (the Shioaji `person_id`): letter, 1 or 2, eight digits. The
# vendor's own session lines embed it (`PYAPI/<id>/...`); this catches it wherever our
# process re-logs such text.
_NATIONAL_ID_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][12]\d{8}(?![A-Za-z0-9])")
_NATIONAL_ID_MASK = "***ID***"


def _mask_kv_value(m: "re.Match[str]") -> str:
    # A value an earlier pass already masked (`token=***TELEGRAM_TOKEN***`) keeps its
    # more specific marker instead of being flattened to the generic one.
    if m.group(2).startswith("***"):
        return m.group(0)
    return m.group(1) + _MASK


def _scrub_value_str(v: str) -> str:
    if "eyJ" in v:
        v = _JWT_RE.sub(_JWT_MASK, v)
    if "earer" in v:
        v = _BEARER_RE.sub(r"\1***", v)
    # Cheap pre-check: a Telegram bot token always contains `:`. Skip regex
    # for the common case (log messages with no colon).
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
            # `error=exc` is rendered by JSONRenderer as repr(exc) AFTER this processor
            # ran, so the message text was never inspected. Scrub the same text now.
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
            # Malformed %-style args: leave the record alone, the handler reports it as
            # a logging error exactly as it did before this factory existed.
            return record
        scrubbed = _scrub_value_str(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = None
        # Render the traceback now so its text is scrubbed too; Formatter.format
        # reuses a pre-set `exc_text` instead of formatting it again.
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
            # format_exc_info MUST precede the scrubber: it turns `exc_info` into the
            # `exception` text, and a scrubber that ran first never saw any traceback.
            structlog.processors.format_exc_info,
            credential_scrubber,
            structlog.processors.JSONRenderer(),
        ],
        # Logs go to stderr so stdout stays a clean channel for machine-readable
        # command output. structlog's default logger factory writes to stdout,
        # which put log lines and JSON payloads on the same stream: ``hft alpha
        # cheap-screen`` emitted a ``cheap_screen_start`` debug line directly in
        # front of its JSON verdict, so piping the command to ``jq`` failed with
        # "Extra data" whenever DEBUG was enabled. Docker's json-file driver
        # captures both streams, so operator-visible logging is unchanged.
        logger_factory=_stderr_logger_factory,
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)
    install_stdlib_scrubber()
    # python-telegram-bot's transport (httpx) logs `HTTP Request: POST
    # https://api.telegram.org/bot<TOKEN>/getUpdates` at INFO on every poll. The scrubber
    # above masks it; not emitting it at all removes the token from the line entirely
    # and a poll-per-second line from the log.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str):
    return structlog.get_logger(name)


# structlog's *own* defaults also print to stdout, and they apply to every log
# line emitted before ``configure_logging`` runs -- at import time, in a unit
# test that calls a command function directly, or in any entry point that logs
# on the way to configuring. Binding the default sink here makes "logs never
# touch stdout" hold regardless of ordering, instead of depending on whoever
# configured logging first. Every module reaches a logger through
# ``get_logger`` above, so importing this module is the earliest common point.
structlog.configure(logger_factory=_stderr_logger_factory)
