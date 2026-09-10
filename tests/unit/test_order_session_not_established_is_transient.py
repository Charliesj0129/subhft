"""A broker session that has not come up yet must be retried, not dropped.

Incident, THESHOW 2026-09-09. The Shioaji SDK reported the order session as
unavailable with::

    place_order: Shioaji error Session error SolClient send request
    api/v1/paper/place_order, code: NotReady, Error ErrorInfo {
    sub_code: SubCode(SessionNotEstablished), error_str: "Unable to wait for
    session '(c4,s1)_sinopac' to be established" }

``OrderAdapter._is_transient_error`` matched five substrings, none of which
appear in that text, so the most obviously retryable condition there is -- a
session still coming up -- was classified permanent. ``_call_api`` skipped its
retry loop and every affected order went to the DLQ on the first attempt.
Measured over the log retention window: 166 occurrences, escalating 6 -> 30 ->
34 -> 96 per day, driving ``dlq_size_total{source="order"}`` from 6 to 31 inside
one night session, tripping the circuit breaker, and firing
``RecorderDeadLetterQueueGrowing`` 55 times in a week -- the single largest
source of production alert traffic while it was active.

The retry is safe here specifically because the SDK raises fast rather than
timing out: the call never reached the broker, so it cannot have placed an
order. That is what separates this from the mutating-timeout case, which
deliberately refuses to retry because the in-flight call may still land.
"""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hft_platform.order.adapter import OrderAdapter

# Verbatim from the production log. Kept whole rather than trimmed: the point of
# the regression is that the real string classifies correctly, and a paraphrase
# would let the same gap reopen unnoticed.
PRODUCTION_ERROR = (
    "place_order: Shioaji error Session error SolClient send request "
    "api/v1/paper/place_order, code: NotReady, Error ErrorInfo { "
    "sub_code: SubCode(SessionNotEstablished), error_str: "
    "\"Unable to wait for session '(c4,s1)_sinopac' to be established\" }"
)


def _make_adapter(tmp_path: Path) -> OrderAdapter:
    cfg_path = tmp_path / "order_cfg.yaml"
    cfg_path.write_text("{}\n")
    return OrderAdapter(str(cfg_path), asyncio.Queue(), MagicMock())


class TestTheSessionErrorClassifiesAsTransient:
    def test_the_verbatim_production_error_is_transient(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        assert adapter._is_transient_error(RuntimeError(PRODUCTION_ERROR)) is True

    @pytest.mark.parametrize(
        "message",
        [
            "SubCode(SessionNotEstablished)",
            "Unable to wait for session to be established",
            "session not established",
            # The SDK is inconsistent about case; the classifier lowercases.
            "SESSIONNOTESTABLISHED",
        ],
    )
    def test_each_session_establishment_phrasing_is_transient(self, tmp_path, message):
        adapter = _make_adapter(tmp_path)
        assert adapter._is_transient_error(RuntimeError(message)) is True

    @pytest.mark.parametrize(
        "message",
        [
            "permanent",
            "Invalid account",
            "insufficient margin",
            "contract not found",
            # A rejection naming the session must NOT be swept in: the broker
            # refusing an order is not a session that has yet to come up.
            "order rejected: session closed for this product",
        ],
    )
    def test_a_genuine_rejection_stays_permanent(self, tmp_path, message):
        """The widened patterns must not turn every failure into a retry loop."""
        adapter = _make_adapter(tmp_path)
        assert adapter._is_transient_error(RuntimeError(message)) is False

    def test_the_original_transient_patterns_still_classify(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        for message in ("ECONNREFUSED", "econnreset", "ETIMEDOUT", "connection reset", "temporarily unavailable"):
            assert adapter._is_transient_error(RuntimeError(message)) is True, message


class TestTheOrderIsRetriedRatherThanDropped:
    """Classification is the mechanism; being retried is the behaviour."""

    @pytest.mark.asyncio
    async def test_a_session_error_that_then_succeeds_returns_the_broker_result(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        calls = {"n": 0}

        def _flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError(PRODUCTION_ERROR)
            return "trade-object"

        result = await adapter._call_api("place_order", _flaky)

        assert calls["n"] == 2, "the first attempt must be retried, not abandoned"
        assert result == "trade-object"

    @pytest.mark.asyncio
    async def test_a_permanent_error_is_still_not_retried(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        calls = {"n": 0}

        def _permanent():
            calls["n"] += 1
            raise RuntimeError("Invalid account")

        result = await adapter._call_api("place_order", _permanent)

        assert calls["n"] == 1
        assert result is None

    @pytest.mark.asyncio
    async def test_a_session_that_never_comes_up_stops_at_the_retry_bound(self, tmp_path):
        """A widened pattern must not become an unbounded retry on a dead session."""
        adapter = _make_adapter(tmp_path)
        calls = {"n": 0}

        def _always_down():
            calls["n"] += 1
            raise RuntimeError(PRODUCTION_ERROR)

        result = await adapter._call_api("place_order", _always_down, max_retries=2)

        assert calls["n"] == 3, "one initial attempt plus max_retries, and no more"
        assert result is None
