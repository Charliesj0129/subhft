"""Every path a secret can take into a log line must go through the scrubber.

The structlog processor already masked *keyword arguments* by name. These tests
pin the paths it did not cover, each found by probing with fake secrets:

  * a traceback: ``format_exc_info`` ran AFTER ``credential_scrubber``, so the
    rendered ``exception`` text was never scrubbed;
  * the Telegram token inside ``https://api.telegram.org/bot<id>:<token>/...``:
    the regex began with ``\\b``, which cannot match between ``t`` and a digit;
  * stdlib loggers (asyncio "Task exception was never retrieved", httpx, ...),
    which never enter the structlog chain;
  * free text such as ``password=...`` inside an exception message;
  * the bot process, which never called ``configure_logging()`` at all.

All "secrets" below are assembled at run time from fragments so no literal in
this file looks like a credential to a scanner.
"""

from __future__ import annotations

import io
import logging
from contextlib import redirect_stderr
from typing import Iterator

import pytest

from hft_platform.utils.logging import configure_logging, credential_scrubber, get_logger

_BOT_ID = "1234567890"
_BOT_SECRET = "AAF" + "x9" * 17  # 37 url-safe chars, the real token shape
_TG_TOKEN = f"{_BOT_ID}:{_BOT_SECRET}"
_PASSWORD = "hunter" + "2-fake"
_NATIONAL_ID = "A" + "1" + "23456789"  # letter, 1|2, eight digits
_JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiJmYWtlIn0" + "." + "c2lnbmF0dXJlLWZha2U"


@pytest.fixture
def stdlib_sink() -> Iterator[io.StringIO]:
    """A root handler that records what the stdlib logging stack would print."""
    configure_logging(level=logging.DEBUG)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield buf
    finally:
        root.removeHandler(handler)


def _render_structlog_error(name: str, exc: BaseException) -> str:
    configure_logging(level=logging.INFO)
    logger = get_logger(name)
    buf = io.StringIO()
    with redirect_stderr(buf):
        try:
            raise exc
        except type(exc):
            logger.error("synthetic_failure", exc_info=True)
    return buf.getvalue()


# --- traceback path -----------------------------------------------------------


def test_traceback_text_masks_password_pair() -> None:
    out = _render_structlog_error("tb-password", RuntimeError(f"login failed password={_PASSWORD}"))
    assert "Traceback" in out, "the traceback must still be rendered"
    assert _PASSWORD not in out


def test_traceback_text_masks_jwt() -> None:
    out = _render_structlog_error("tb-jwt", RuntimeError(f"rpc rejected {_JWT}"))
    assert "Traceback" in out
    assert _JWT not in out


def test_traceback_keeps_the_exception_type_and_unrelated_message() -> None:
    out = _render_structlog_error("tb-keeps", ValueError("contract TXFJ6 not found"))
    assert "ValueError" in out
    assert "contract TXFJ6 not found" in out


def test_exception_object_passed_as_a_field_is_scrubbed() -> None:
    """`error=exc` is rendered as repr(exc) after the processor ran; its text must be scrubbed."""
    out = credential_scrubber(None, "error", {"error": RuntimeError(f"login failed password={_PASSWORD}")})
    assert _PASSWORD not in str(out["error"])
    assert "RuntimeError" in str(out["error"])


# --- telegram token ------------------------------------------------------------


def test_telegram_token_in_bot_url_is_masked() -> None:
    url = f"https://api.telegram.org/bot{_TG_TOKEN}/getUpdates"
    out = credential_scrubber(None, "info", {"event": f"HTTP Request: POST {url} 200"})
    assert _BOT_SECRET not in out["event"]
    assert "api.telegram.org" in out["event"], "only the token is masked, not the whole line"


def test_telegram_token_standalone_is_still_masked() -> None:
    out = credential_scrubber(None, "info", {"event": f"token is {_TG_TOKEN} ok"})
    assert _BOT_SECRET not in out["event"]


# --- free text pairs and identifiers --------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        f"password={_PASSWORD}",
        f'"password": "{_PASSWORD}"',
        f"{{'password': '{_PASSWORD}'}}",
        f"SHIOAJI_SECRET_KEY={_PASSWORD}",
        f"ca_passwd: {_PASSWORD}",
        f"api_key = {_PASSWORD}",
        f"https://host/path?token={_PASSWORD}&x=1",
    ],
)
def test_free_text_secret_pair_is_masked(text: str) -> None:
    out = credential_scrubber(None, "error", {"error": f"failed: {text}"})
    assert _PASSWORD not in out["error"], out["error"]


def test_quoted_secret_with_spaces_is_masked_whole() -> None:
    out = credential_scrubber(None, "error", {"error": 'bad password="two words here"'})
    assert "words" not in out["error"], out["error"]


@pytest.mark.parametrize("text", ["TokenExpiredError: session expired", "max_tokens=5 used", "contract TXFJ6 x=1"])
def test_unrelated_text_is_left_alone(text: str) -> None:
    out = credential_scrubber(None, "info", {"event": text})
    assert out["event"] == text


def test_person_id_keyword_is_masked_by_name() -> None:
    out = credential_scrubber(None, "info", {"person_id": _NATIONAL_ID, "ca_passwd": _PASSWORD})
    assert _NATIONAL_ID not in str(out) and _PASSWORD not in str(out)


def test_national_id_shape_in_free_text_is_masked() -> None:
    out = credential_scrubber(None, "info", {"event": f"client PYAPI/{_NATIONAL_ID}/1003 ok"})
    assert _NATIONAL_ID not in out["event"]


# --- stdlib logging bypass -------------------------------------------------------


def test_stdlib_message_is_scrubbed(stdlib_sink: io.StringIO) -> None:
    logging.getLogger("some.library").error("handshake rejected for %s", f"password={_PASSWORD}")
    assert _PASSWORD not in stdlib_sink.getvalue()
    assert "handshake rejected" in stdlib_sink.getvalue()


def test_stdlib_traceback_is_scrubbed(stdlib_sink: io.StringIO) -> None:
    log = logging.getLogger("asyncio")
    try:
        raise RuntimeError(f"Task exception was never retrieved: {_JWT} token={_PASSWORD}")
    except RuntimeError:
        log.exception("Task exception was never retrieved")
    out = stdlib_sink.getvalue()
    assert "Traceback" in out
    assert _JWT not in out and _PASSWORD not in out


def test_stdlib_telegram_url_is_scrubbed(stdlib_sink: io.StringIO) -> None:
    logging.getLogger("httpx").warning("HTTP Request: POST https://api.telegram.org/bot%s/getMe", _TG_TOKEN)
    assert _BOT_SECRET not in stdlib_sink.getvalue()


def test_stdlib_record_with_malformed_args_is_left_untouched(stdlib_sink: io.StringIO) -> None:
    """The scrubbing record factory must not turn a caller's bad %-args into a crash,
    nor rewrite a record it cannot format: the handler reports it as it always did."""
    record = logging.getLogRecordFactory()(
        "some.library", logging.ERROR, __file__, 1, "two slots %s %s", ("one-arg",), None
    )
    assert record.msg == "two slots %s %s"
    assert record.args == ("one-arg",)


def test_http_client_loggers_are_not_at_info() -> None:
    """python-telegram-bot's transport writes the token URL at INFO.

    Under pytest ``basicConfig`` is a no-op (the runner already installed root
    handlers) so the root level would stay at WARNING and this assertion would
    pass whatever the code does. Pin the root to INFO first, as production has it.
    """
    names = ("", "httpx", "httpcore")
    saved = {n: logging.getLogger(n).level for n in names}
    try:
        logging.getLogger().setLevel(logging.INFO)
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.NOTSET)
        assert logging.getLogger("httpx").isEnabledFor(logging.INFO), "precondition: INFO is enabled"

        configure_logging(level=logging.INFO)

        for name in ("httpx", "httpcore"):
            assert not logging.getLogger(name).isEnabledFor(logging.INFO), name
    finally:
        for n, lvl in saved.items():
            logging.getLogger(n).setLevel(lvl)


# --- the bot process --------------------------------------------------------------


def test_bot_main_configures_logging_before_building_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    from hft_platform.bot import app as bot_app

    order: list[str] = []

    class _FakeApp:
        def run_polling(self, **_kw: object) -> None:
            order.append("run_polling")

    monkeypatch.setattr(bot_app, "configure_logging", lambda *a, **k: order.append("configure_logging"), raising=False)
    monkeypatch.setattr(bot_app, "_start_health_server_background", lambda: order.append("health"))
    monkeypatch.setattr(bot_app, "create_app", lambda: (order.append("create_app"), _FakeApp())[1])

    bot_app.main()

    assert order[0] == "configure_logging", order
    assert order.index("configure_logging") < order.index("create_app")
