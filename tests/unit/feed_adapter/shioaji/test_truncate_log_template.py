"""Fix 2 regression: subscription-truncate log carries conn_id, shard_size,
dropped_sample, and actionable hint.

Prior template ("Subscription limit reached", severity="critical") obscured
the 2026-05-23 shard-overwrite bug because it omitted ``conn_id`` (operator
could not tell which connection truncated) and used the misleading field
``requested`` (which is really the per-facade shard size, not the user's
requested universe).
"""

from __future__ import annotations

from structlog.testing import capture_logs

from hft_platform.feed_adapter.shioaji.subscription_manager import _log_truncate_event


class _StubClient:
    MAX_SUBSCRIPTIONS = 120

    def __init__(self, *, conn_id, symbols, subscribed):
        self.conn_id = conn_id
        self.symbols = symbols
        self.subscribed_count = subscribed


def _make_symbols(n: int) -> list[dict[str, str]]:
    return [{"code": f"SYM{i:04d}", "exchange": "TSE"} for i in range(n)]


def _emit(client, *, requested: int, phase: str) -> dict:
    """Return the truncate log as an event dict.

    These assertions used to read ``capsys``, which made them depend on the
    process-wide structlog renderer: they passed only when some *earlier* test
    in the run had installed the key-value renderer, and failed in isolation
    against the coloured console one. They also drained the capture twice --
    ``readouterr().out + readouterr().err`` returns an always-empty ``err``,
    because the first call consumes the buffer.

    ``capture_logs`` asserts on the structured record the code actually emits,
    which is the contract being tested; how it is rendered is not.
    """
    with capture_logs() as records:
        _log_truncate_event(client, requested=requested, phase=phase)
    assert records, "no log record was emitted"
    return records[-1]


def test_truncate_log_binds_conn_id_and_records_shard_size():
    client = _StubClient(conn_id="2", symbols=_make_symbols(478), subscribed=120)
    record = _emit(client, requested=478, phase="resubscribe")
    assert record["event"] == "subscription_limit_reached"
    assert str(record["conn_id"]) == "2"
    assert record["shard_size"] == 478
    assert record["subscribed_this_facade"] == 120
    assert record["dropped_this_facade"] == 358
    assert record["phase"] == "resubscribe"


def test_truncate_log_includes_dropped_sample():
    client = _StubClient(conn_id="0", symbols=_make_symbols(150), subscribed=120)
    record = _emit(client, requested=150, phase="subscribe_basket")
    text = str(record)
    # First 5 dropped codes (indices 120..124)
    for i in range(120, 125):
        assert f"SYM{i:04d}" in text, f"expected dropped sample SYM{i:04d} in: {text!r}"


def test_truncate_log_severity_is_warning_not_error():
    """Bug A's log emitted ``severity="critical"`` as a free-text tag with
    no consumer. We downgraded to ``warning`` and removed the tag."""
    client = _StubClient(conn_id="3", symbols=_make_symbols(200), subscribed=120)
    record = _emit(client, requested=200, phase="resubscribe")
    assert "severity" not in record
    assert record["log_level"] == "warning"


def test_truncate_log_hint_mentions_shard_integrity():
    client = _StubClient(conn_id="1", symbols=_make_symbols(478), subscribed=120)
    text = str(_emit(client, requested=478, phase="resubscribe"))
    assert "shard integrity" in text or "shard_integrity" in text
    assert "refresh_contracts_and_symbols" in text


def test_truncate_log_handles_missing_conn_id():
    """A facade not built by QuoteConnectionPool (single-conn legacy mode)
    has no ``conn_id`` attribute. The helper must default to ``unknown``
    rather than crash, so the log is still emitted."""

    class _LegacyClient:
        MAX_SUBSCRIPTIONS = 120
        symbols = _make_symbols(140)
        subscribed_count = 120

    record = _emit(_LegacyClient(), requested=140, phase="subscribe_basket")
    assert record["conn_id"] == "unknown"
