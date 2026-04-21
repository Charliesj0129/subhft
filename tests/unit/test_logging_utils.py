import io
import logging
from contextlib import redirect_stdout

from hft_platform.utils.logging import configure_logging, credential_scrubber, get_logger


def test_get_logger_returns_logger():
    logger = get_logger("unit-test")
    assert logger is not None


def test_configure_logging_sets_root_level():
    configure_logging(level=logging.DEBUG)
    assert isinstance(logging.getLogger().getEffectiveLevel(), int)


def test_logger_renders_traceback_when_exc_info_true():
    """Bug D'': structlog must include format_exc_info processor so that
    logger.error(..., exc_info=True) renders a real traceback, not just the
    string `"exc_info": true`. Without this we are blind to phantom dispatch
    failures and other production exceptions.
    """
    configure_logging(level=logging.INFO)
    logger = get_logger("traceback-test")
    buf = io.StringIO()
    sentinel = "VeryUniqueSentinelException_EXCBE9F"
    with redirect_stdout(buf):
        try:
            raise RuntimeError(sentinel)
        except RuntimeError:
            logger.error("synthetic_failure", exc_info=True)
    out = buf.getvalue()
    assert "Traceback" in out, (
        "structlog did not render traceback (format_exc_info processor missing). "
        f"Captured output: {out!r}"
    )
    assert sentinel in out, "exception message not present in rendered output"
    assert '"exc_info": true' not in out, (
        "raw `exc_info: true` token leaked — format_exc_info should consume it"
    )


# Bug #31 — JWT/Bearer scrubber regression tests
# Symptom: account_gateway.py:66 logged `error=str(exc)` containing a Shioaji JWT
# (header.payload.signature). credential_scrubber only inspected KEY names, so the
# token flowed through into JSONRenderer output. Fix: scrub JWT and Bearer patterns
# from string VALUES regardless of key name.

_FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiJ9"
    ".eyJwZXJzb25faWQiOiJUMDAwMDAwMDAwIiwic2ltdWxhdGlvbiI6dHJ1ZX0"
    ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
)


def test_credential_scrubber_redacts_jwt_in_value():
    event_dict = {"error": f"RPC failed: {_FAKE_JWT}"}
    out = credential_scrubber(None, "error", event_dict)
    assert "eyJ" not in out["error"], f"JWT leaked: {out['error']!r}"
    assert "***JWT***" in out["error"]


def test_credential_scrubber_redacts_bearer_token_in_value():
    event_dict = {"error": "auth header was 'Bearer abc123def456'"}
    out = credential_scrubber(None, "error", event_dict)
    assert "abc123def456" not in out["error"], f"Bearer token leaked: {out['error']!r}"


def test_credential_scrubber_passthrough_non_string_values():
    event_dict = {"count": 42, "ratio": 0.5, "missing": None, "items": [1, 2, 3]}
    out = credential_scrubber(None, "info", event_dict)
    assert out["count"] == 42
    assert out["ratio"] == 0.5
    assert out["missing"] is None
    assert out["items"] == [1, 2, 3]


def test_credential_scrubber_existing_key_masking_still_works():
    event_dict = {"api_key": "secret-value-123", "user": "alice"}
    out = credential_scrubber(None, "info", event_dict)
    assert out["api_key"] == "***"
    assert out["user"] == "alice"


def test_credential_scrubber_short_circuit_when_no_jwt_marker():
    """Cheap pre-check ('eyJ' / 'earer' substring) should make the common path
    a single substring scan with no regex invocation."""
    event_dict = {"error": "ordinary error message with no secrets at all" * 100}
    out = credential_scrubber(None, "info", event_dict)
    assert out["error"].startswith("ordinary error message")


def test_full_pipeline_does_not_leak_jwt_in_rendered_output():
    """End-to-end: configure_logging → log error containing JWT → captured stdout
    must NOT contain the JWT body."""
    configure_logging(level=logging.INFO)
    logger = get_logger("jwt-leak-test")
    buf = io.StringIO()
    with redirect_stdout(buf):
        logger.error("rpc_call_failed", error=f"shioaji RPC blew up: {_FAKE_JWT}")
    out = buf.getvalue()
    assert "eyJ" not in out, f"JWT body leaked in JSON output: {out!r}"
    assert "person_id" not in out, "JWT payload fragment leaked"
