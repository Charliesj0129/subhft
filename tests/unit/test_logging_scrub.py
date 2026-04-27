"""Token/secret scrub regression tests (P0-b 2026-04-27).

Background: a prior Infra investigator session accidentally emitted a live
Telegram bot token via structlog (field name `telegram_token`). The previous
``credential_scrubber`` only covered ``api_key`` / ``secret_key`` / ``password``
key-name patterns and JWT/Bearer value patterns — the literal Telegram bot
token format `<digits>:<base64url>` was NOT redacted, neither by key name
(`telegram_token` did not match) nor by value pattern.

These tests pin the new behavior:
  1. Key names containing ``bot_token`` / ``BOT_TOKEN`` are masked regardless
     of the value type.
  2. String VALUES that match the Telegram bot-token regex are masked even
     when the key name itself is innocuous (e.g. ``event``, ``msg``, ``error``).
"""

from __future__ import annotations

import io
import logging
from contextlib import redirect_stdout

import pytest

from hft_platform.utils.logging import (
    _MASK,
    _TELEGRAM_TOKEN_MASK,
    configure_logging,
    credential_scrubber,
    get_logger,
)

# Realistic shape — a numeric bot id followed by ``:`` and ~35 url-safe-base64
# characters. NOT a real token. Long enough to match `[A-Za-z0-9_-]{30,}`.
_FAKE_TELEGRAM_TOKEN = "8794586948:AAFP1234567890abcdefghijklmnopqrstuv"


def test_scrubber_redacts_telegram_token_when_key_is_telegram_token() -> None:
    out = credential_scrubber(None, "info", {"telegram_token": _FAKE_TELEGRAM_TOKEN})
    assert out["telegram_token"] == _MASK


def test_scrubber_redacts_telegram_token_when_key_uppercase() -> None:
    out = credential_scrubber(
        None, "info", {"HFT_TELEGRAM_BOT_TOKEN": _FAKE_TELEGRAM_TOKEN}
    )
    assert out["HFT_TELEGRAM_BOT_TOKEN"] == _MASK


def test_scrubber_redacts_telegram_token_in_free_form_value() -> None:
    """Even when the KEY name is innocent (`event`, `msg`, `error`), a
    Telegram-formatted bot token in the VALUE string MUST be redacted."""
    msg = f"investigator probe: token={_FAKE_TELEGRAM_TOKEN}"
    out = credential_scrubber(None, "info", {"event": msg})
    assert _FAKE_TELEGRAM_TOKEN not in out["event"]
    assert _TELEGRAM_TOKEN_MASK in out["event"]


def test_scrubber_passes_through_strings_with_colon_but_no_token() -> None:
    """Cheap pre-check (`:` in v) must not over-redact. A colon-bearing string
    with no token-format substring stays intact."""
    msg = "ratio=0.5: file=foo.txt :: 12:34 timestamp"
    out = credential_scrubber(None, "info", {"event": msg})
    assert out["event"] == msg


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "secret_key",
        "password",
        "bot_token",
        "BOT_TOKEN",
        "BotToken",
    ],
)
def test_scrubber_masks_known_secret_key_names(key: str) -> None:
    out = credential_scrubber(None, "info", {key: "anything-here"})
    assert out[key] == _MASK


def test_full_pipeline_does_not_leak_telegram_token_in_field() -> None:
    """End-to-end: configure_logging → log dict containing a Telegram token →
    captured stdout must NOT contain the token body."""
    configure_logging(level=logging.INFO)
    logger = get_logger("telegram-leak-test")
    buf = io.StringIO()
    with redirect_stdout(buf):
        logger.error("rpc_call_failed", telegram_token=_FAKE_TELEGRAM_TOKEN)
    out = buf.getvalue()
    assert _FAKE_TELEGRAM_TOKEN not in out, f"telegram token leaked in JSON: {out!r}"
    # Token mask OR generic mask is fine — both indicate redaction succeeded.
    assert ("***" in out)


def test_full_pipeline_does_not_leak_telegram_token_in_message() -> None:
    """End-to-end: token embedded in a free-form value (event=...) must also
    be masked by the value-scrubbing path."""
    configure_logging(level=logging.INFO)
    logger = get_logger("telegram-leak-msg-test")
    buf = io.StringIO()
    with redirect_stdout(buf):
        logger.error(
            "investigator_probe", note=f"telegram_token={_FAKE_TELEGRAM_TOKEN}"
        )
    out = buf.getvalue()
    assert _FAKE_TELEGRAM_TOKEN not in out, f"telegram token leaked in JSON: {out!r}"
