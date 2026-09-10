"""A dead order session must be re-authenticated, not retried forever.

Incident, THESHOW 2026-09-08 to 09-10. The order path runs on its own
``ShioajiClientFacade``, built alongside but separately from the quote pool.
The quote side has a watchdog, a forced-relogin path and a reconnect
orchestrator. The order side was logged in exactly once, in
``HFTSystem.run``, and never again.

The session went down and stayed down for 45 hours across zero container
restarts. The feed stayed healthy the whole time -- roughly 38 ms gaps --
because that is a different session. ``dlq_size_total{source="order"}`` went
from 6 to 1,692 and ``pipeline_latency_ns_count{stage="api_place_order"}`` did
not move once.

Two traps these tests pin down:

- ``order_client.is_connected()`` returns ``logged_in and api is not None``.
  Both stayed true for the entire outage, so health cannot be read from it.
  The only honest signal is that orders keep dying.
- ``ReconnectOrchestrator.reconnect`` restores *quote* callbacks and
  re-subscribes quotes. It does not touch execution callbacks. A reconnect
  that forgets to re-register them leaves the session authenticated and
  placing orders while every fill goes nowhere -- worse than not trading.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hft_platform.order.adapter import OrderAdapter
from hft_platform.services.system import HFTSystem

SESSION_ERROR = (
    "place_order: Shioaji error Session error SolClient send request "
    "api/v1/paper/place_order, code: NotReady, Error ErrorInfo { "
    "sub_code: SubCode(SessionNotEstablished), error_str: "
    "\"Unable to wait for session '(c4,s1)_sinopac' to be established\" }"
)


def _make_adapter(tmp_path: Path) -> OrderAdapter:
    cfg_path = tmp_path / "order_cfg.yaml"
    cfg_path.write_text("{}\n")
    return OrderAdapter(str(cfg_path), asyncio.Queue(), MagicMock())


class TestTheAdapterCountsOnlyLostOrders:
    def test_a_fresh_adapter_reports_no_session_errors(self, tmp_path):
        assert _make_adapter(tmp_path).consecutive_session_errors == 0

    @pytest.mark.asyncio
    async def test_one_lost_order_counts_once_not_once_per_retry(self, tmp_path):
        """Counting per attempt would trip the watchdog at a third of its threshold."""
        adapter = _make_adapter(tmp_path)
        calls = {"n": 0}

        def _always_down():
            calls["n"] += 1
            raise RuntimeError(SESSION_ERROR)

        await adapter._call_api("place_order", _always_down, max_retries=2)

        assert calls["n"] == 3, "the retry loop must still run"
        assert adapter.consecutive_session_errors == 1

    @pytest.mark.asyncio
    async def test_consecutive_losses_accumulate(self, tmp_path):
        adapter = _make_adapter(tmp_path)

        def _always_down():
            raise RuntimeError(SESSION_ERROR)

        for _ in range(3):
            await adapter._call_api("place_order", _always_down, max_retries=0)

        assert adapter.consecutive_session_errors == 3

    @pytest.mark.asyncio
    async def test_any_success_clears_the_count(self, tmp_path):
        """A session that answers is up; the watchdog must not relogin it."""
        adapter = _make_adapter(tmp_path)

        def _down():
            raise RuntimeError(SESSION_ERROR)

        await adapter._call_api("place_order", _down, max_retries=0)
        assert adapter.consecutive_session_errors == 1

        await adapter._call_api("cancel_order", lambda: "ok")
        assert adapter.consecutive_session_errors == 0, "a successful cancel proves the session is alive"

    @pytest.mark.asyncio
    async def test_an_ordinary_rejection_does_not_count(self, tmp_path):
        """Only a session that never came up justifies re-authenticating."""
        adapter = _make_adapter(tmp_path)

        def _rejected():
            raise RuntimeError("insufficient margin")

        await adapter._call_api("place_order", _rejected, max_retries=0)

        assert adapter.consecutive_session_errors == 0

    @pytest.mark.asyncio
    async def test_a_timeout_does_not_count_as_a_dead_session(self, tmp_path):
        """A broker that answers slowly is not a broker that never logged in."""
        adapter = _make_adapter(tmp_path)

        def _slow():
            raise asyncio.TimeoutError()

        await adapter._call_api("place_order", _slow, max_retries=0)

        assert adapter.consecutive_session_errors == 0


class TestTheSessionErrorClassifierIsNarrowerThanTransient:
    @pytest.mark.parametrize(
        "message",
        ["SubCode(SessionNotEstablished)", "session not established", "unable to wait for it to be established"],
    )
    def test_session_errors_are_both_transient_and_session_errors(self, tmp_path, message):
        adapter = _make_adapter(tmp_path)
        exc = RuntimeError(message)
        assert adapter._is_transient_error(exc) is True
        assert adapter._is_session_establishment_error(exc) is True

    @pytest.mark.parametrize("message", ["ECONNRESET", "connection reset", "temporarily unavailable"])
    def test_other_transient_errors_are_not_session_errors(self, tmp_path, message):
        """A blip must not spend a login slot re-authenticating a live session."""
        adapter = _make_adapter(tmp_path)
        exc = RuntimeError(message)
        assert adapter._is_transient_error(exc) is True
        assert adapter._is_session_establishment_error(exc) is False


class _StubSystem:
    """Just enough HFTSystem to exercise the session decision.

    Bound rather than constructed: a real HFTSystem needs a broker, a
    recorder and a ClickHouse connection, none of which this decision touches.
    """

    _check_order_session_once = HFTSystem._check_order_session_once

    def __init__(self, *, failures: int, reconnect_result=True, with_callbacks: bool = True):
        self.order_adapter = SimpleNamespace(consecutive_session_errors=failures)
        self.reconnect_calls: list[dict] = []
        self.registered: list[tuple] = []

        def _reconnect(reason: str = "", force: bool = False):
            self.reconnect_calls.append({"reason": reason, "force": force})
            if isinstance(reconnect_result, Exception):
                raise reconnect_result
            return reconnect_result

        def _set_execution_callbacks(on_order, on_deal):
            self.registered.append((on_order, on_deal))

        self.order_client = SimpleNamespace(
            reconnect=_reconnect,
            set_execution_callbacks=_set_execution_callbacks,
            # True throughout the real outage, which is why nothing may read it.
            is_connected=lambda: True,
        )
        if with_callbacks:
            self._exec_callbacks = (lambda state, payload: None, lambda payload: None)


class TestTheWatchdogOnlyActsOnASustainedFailure:
    @pytest.mark.asyncio
    async def test_a_healthy_session_is_left_alone(self):
        system = _StubSystem(failures=0)
        assert await system._check_order_session_once(threshold=3) == "healthy"
        assert system.reconnect_calls == []

    @pytest.mark.asyncio
    async def test_failures_below_the_threshold_do_not_relogin(self):
        """One blip must not spend a login slot; the retry loop handles those."""
        system = _StubSystem(failures=2)
        assert await system._check_order_session_once(threshold=3) == "healthy"
        assert system.reconnect_calls == []

    @pytest.mark.asyncio
    async def test_reaching_the_threshold_reconnects(self):
        system = _StubSystem(failures=3)
        assert await system._check_order_session_once(threshold=3) == "reconnected"
        assert len(system.reconnect_calls) == 1
        assert system.reconnect_calls[0]["reason"] == "order_session_not_established"

    @pytest.mark.asyncio
    async def test_the_reconnect_is_not_forced(self):
        """force=True bypasses cooldown and backoff, which is how a dead
        session becomes a relogin storm and every facade earns a 451."""
        system = _StubSystem(failures=9)
        await system._check_order_session_once(threshold=3)
        assert system.reconnect_calls[0]["force"] is False


class TestTheFillPathIsRestoredOrTradingStops:
    @pytest.mark.asyncio
    async def test_execution_callbacks_are_reregistered_after_a_reconnect(self):
        """The reconnect orchestrator restores quote callbacks only."""
        system = _StubSystem(failures=3)
        await system._check_order_session_once(threshold=3)
        assert len(system.registered) == 1
        assert system.registered[0] == system._exec_callbacks

    @pytest.mark.asyncio
    async def test_a_reconnect_with_no_callbacks_to_restore_is_reported_not_ignored(self):
        """Placing orders whose fills go nowhere is worse than not trading."""
        system = _StubSystem(failures=3, with_callbacks=False)
        assert await system._check_order_session_once(threshold=3) == "no_callbacks"
        assert system.registered == []

    @pytest.mark.asyncio
    async def test_a_gated_reconnect_does_not_touch_the_callbacks(self):
        """reconnect() returns False when cooled down; the session is untouched."""
        system = _StubSystem(failures=3, reconnect_result=False)
        assert await system._check_order_session_once(threshold=3) == "reconnect_failed"
        assert system.registered == []

    @pytest.mark.asyncio
    async def test_a_client_that_cannot_reconnect_is_reported(self):
        system = _StubSystem(failures=3)
        del system.order_client.reconnect
        assert await system._check_order_session_once(threshold=3) == "cannot_reconnect"
        assert system.registered == []
