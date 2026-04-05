"""Tests for _ch_poller.py assert-replacement fix (P1-3).

Verifies that when fetch_recent_valid exits the retry loop with last_exc=None
(e.g., loop never executes because initial replay_limit < 8), a ConnectionError
is raised — not AssertionError — so the monitor process never crashes in production.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.monitor._ch_poller import CHPoller


class TestCHPollerLastExcNonePath:
    """Exercises the last_exc is None guard introduced to replace the assert."""

    def _make_poller(self) -> CHPoller:
        poller = CHPoller(
            host="localhost",
            port=8123,
            symbols=("2330",),
        )
        # Inject a fake client so _client is not None
        poller._client = MagicMock()
        return poller

    def test_raises_connection_error_not_assertion_error_when_last_exc_none(
        self,
    ) -> None:
        """If the while loop never assigns last_exc, ConnectionError must be raised."""
        poller = self._make_poller()

        # Force replay_limit to start below 8 so the while loop body never executes.
        # We achieve this by patching max() to return a value < 8 only for the
        # initial replay_limit computation, making the while condition false immediately.
        # Simpler: call with limit=0 so max(1, int(0))=1 which is < 8 → loop skips.
        with pytest.raises(ConnectionError, match="no cursors executed"):
            poller.fetch_recent_valid("2330", limit=0)

    def test_raises_connection_error_not_assertion_error_type(self) -> None:
        """Verify the raised exception is ConnectionError, never AssertionError."""
        poller = self._make_poller()

        exc_type: type[BaseException] | None = None
        try:
            poller.fetch_recent_valid("2330", limit=0)
        except BaseException as exc:
            exc_type = type(exc)

        assert exc_type is ConnectionError

    def test_error_message_contains_diagnostic(self) -> None:
        """ConnectionError message must identify the no-cursors-executed condition."""
        poller = self._make_poller()

        with pytest.raises(ConnectionError) as exc_info:
            poller.fetch_recent_valid("2330", limit=0)

        assert "no cursors executed" in str(exc_info.value)

    def test_last_exc_set_by_exception_propagates_correctly(self) -> None:
        """If the loop runs and sets last_exc, that exception is chained into the raise."""
        poller = self._make_poller()

        original_error = RuntimeError("MEMORY_LIMIT_EXCEEDED injected")
        poller._client.query.side_effect = original_error  # type: ignore[union-attr]

        with pytest.raises(ConnectionError, match="CH replay failed") as exc_info:
            # replay_limit=8 so loop runs once; exception is not MEMORY_LIMIT_EXCEEDED
            # enough to halve (replay_limit <= 8 short-circuits), so last_exc is set.
            poller.fetch_recent_valid("2330", limit=8)

        assert exc_info.value.__cause__ is original_error
