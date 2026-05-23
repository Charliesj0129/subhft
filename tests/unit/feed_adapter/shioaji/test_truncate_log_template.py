"""Fix 2 regression: subscription-truncate log carries conn_id, shard_size,
dropped_sample, and actionable hint.

Prior template ("Subscription limit reached", severity="critical") obscured
the 2026-05-23 shard-overwrite bug because it omitted ``conn_id`` (operator
could not tell which connection truncated) and used the misleading field
``requested`` (which is really the per-facade shard size, not the user's
requested universe).
"""

from __future__ import annotations

from hft_platform.feed_adapter.shioaji.subscription_manager import _log_truncate_event


class _StubClient:
    MAX_SUBSCRIPTIONS = 120

    def __init__(self, *, conn_id, symbols, subscribed):
        self.conn_id = conn_id
        self.symbols = symbols
        self.subscribed_count = subscribed


def _make_symbols(n: int) -> list[dict[str, str]]:
    return [{"code": f"SYM{i:04d}", "exchange": "TSE"} for i in range(n)]


def test_truncate_log_binds_conn_id_and_records_shard_size(capsys):
    client = _StubClient(conn_id="2", symbols=_make_symbols(478), subscribed=120)
    _log_truncate_event(client, requested=478, phase="resubscribe")
    out = capsys.readouterr().out + capsys.readouterr().err
    # Re-read since the second readouterr drained captured buffer; combine.
    text = out
    assert "subscription_limit_reached" in text
    assert "conn_id=2" in text
    assert "shard_size=478" in text
    assert "subscribed_this_facade=120" in text
    assert "dropped_this_facade=358" in text
    assert "phase=resubscribe" in text


def test_truncate_log_includes_dropped_sample(capsys):
    client = _StubClient(conn_id="0", symbols=_make_symbols(150), subscribed=120)
    _log_truncate_event(client, requested=150, phase="subscribe_basket")
    text = capsys.readouterr().out + capsys.readouterr().err
    # First 5 dropped codes (indices 120..124)
    for i in range(120, 125):
        assert f"SYM{i:04d}" in text, f"expected dropped sample SYM{i:04d} in: {text!r}"


def test_truncate_log_severity_is_warning_not_error(capsys):
    """Bug A's log emitted ``severity="critical"`` as a free-text tag with
    no consumer. We downgraded to ``warning`` and removed the tag."""
    client = _StubClient(conn_id="3", symbols=_make_symbols(200), subscribed=120)
    _log_truncate_event(client, requested=200, phase="resubscribe")
    text = capsys.readouterr().out + capsys.readouterr().err
    assert "severity=critical" not in text
    # structlog default key_value renderer emits ``level=warning`` for warning logs
    assert "level=warning" in text or "[warning" in text.lower()


def test_truncate_log_hint_mentions_shard_integrity(capsys):
    client = _StubClient(conn_id="1", symbols=_make_symbols(478), subscribed=120)
    _log_truncate_event(client, requested=478, phase="resubscribe")
    text = capsys.readouterr().out + capsys.readouterr().err
    assert "shard integrity" in text or "shard_integrity" in text
    assert "refresh_contracts_and_symbols" in text


def test_truncate_log_handles_missing_conn_id(capsys):
    """A facade not built by QuoteConnectionPool (single-conn legacy mode)
    has no ``conn_id`` attribute. The helper must default to ``unknown``
    rather than crash, so the log is still emitted."""

    class _LegacyClient:
        MAX_SUBSCRIPTIONS = 120
        symbols = _make_symbols(140)
        subscribed_count = 120

    _log_truncate_event(_LegacyClient(), requested=140, phase="subscribe_basket")
    text = capsys.readouterr().out + capsys.readouterr().err
    assert "conn_id=unknown" in text
