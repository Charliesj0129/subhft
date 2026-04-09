"""Tests for 7 targeted fixes across observability and analytics modules.

Fix list:
  T3: distributor.py — asyncio.get_event_loop() → asyncio.get_running_loop()
  T4: latency.py — Thread-safe singleton with double-checked locking
  T5: dispatcher.py — _send_critical() uses asyncio.gather() for parallel delivery
  T6: templates.py — heartbeat pnl_scaled displayed as NTD (// 10000)
  T7: alertmanager_bridge.py — Content-Length cap at 1 MB (returns 413)
  T9: latency.py — Integer division latency_ns // 1000 (not float division)
  T10: slippage.py — commission fallback uses max(0, fee_ntd - tax_ntd)
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import unittest.mock as mock

import pytest

# ---------------------------------------------------------------------------
# T3: distributor.py — get_running_loop() usage
# ---------------------------------------------------------------------------


def test_distributor_uses_get_running_loop_not_get_event_loop() -> None:
    """ReportSender._do_post must call get_running_loop, not get_event_loop."""
    import hft_platform.reports.distributor as distributor_module

    source = inspect.getsource(distributor_module.ReportSender._do_post)
    assert "get_running_loop" in source
    assert "get_event_loop" not in source


def test_distributor_multipart_post_uses_get_running_loop() -> None:
    """ReportSender._do_multipart_post must also use get_running_loop."""
    import hft_platform.reports.distributor as distributor_module

    source = inspect.getsource(distributor_module.ReportSender._do_multipart_post)
    assert "get_running_loop" in source
    assert "get_event_loop" not in source


# ---------------------------------------------------------------------------
# T4: latency.py — Thread-safe singleton with double-checked locking
# ---------------------------------------------------------------------------


def test_latency_recorder_singleton_has_instance_lock() -> None:
    """LatencyRecorder class must expose a threading.Lock as _instance_lock."""
    from hft_platform.observability.latency import LatencyRecorder

    assert hasattr(LatencyRecorder, "_instance_lock"), "_instance_lock attribute missing"
    assert isinstance(LatencyRecorder._instance_lock, type(threading.Lock())), "_instance_lock must be a threading.Lock"


def test_latency_recorder_get_returns_same_instance() -> None:
    """LatencyRecorder.get() must always return the identical singleton object."""
    from hft_platform.observability.latency import LatencyRecorder

    LatencyRecorder.reset_for_tests()
    instance_a = LatencyRecorder.get()
    instance_b = LatencyRecorder.get()
    assert instance_a is instance_b, "get() must return the same singleton instance"


def test_latency_recorder_singleton_thread_safety() -> None:
    """Concurrent calls to LatencyRecorder.get() must all return the same instance."""
    from hft_platform.observability.latency import LatencyRecorder

    LatencyRecorder.reset_for_tests()
    results: list[LatencyRecorder] = []
    barrier = threading.Barrier(10)

    def grab_instance() -> None:
        barrier.wait()
        results.append(LatencyRecorder.get())

    threads = [threading.Thread(target=grab_instance) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    first = results[0]
    assert all(r is first for r in results), "All threads must receive the same singleton"


def test_latency_recorder_double_checked_locking_in_source() -> None:
    """The get() method source must contain double-checked locking pattern."""
    from hft_platform.observability.latency import LatencyRecorder

    source = inspect.getsource(LatencyRecorder.get)
    # Expect two checks: outer `if cls._instance is None` and inner after lock acquisition
    assert source.count("_instance is None") >= 2, (
        "get() must contain double-checked locking (two _instance is None checks)"
    )
    assert "_instance_lock" in source, "get() must acquire _instance_lock"


# ---------------------------------------------------------------------------
# T5: dispatcher.py — _send_critical() parallel delivery via asyncio.gather
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_critical_calls_both_primary_and_fallback() -> None:
    """_send_critical must invoke both sender.send and fallback_sender.send."""
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    primary_calls: list[str] = []
    fallback_calls: list[str] = []

    async def primary_send(msg: str, *, critical: bool = False) -> None:
        primary_calls.append(msg)

    async def fallback_send(msg: str) -> None:
        fallback_calls.append(msg)

    primary = mock.AsyncMock()
    primary.send = primary_send
    fallback = mock.AsyncMock()
    fallback.send = fallback_send

    dispatcher = NotificationDispatcher(sender=primary, fallback_sender=fallback)
    await dispatcher._send_critical("test-critical-message")

    assert len(primary_calls) == 1, "primary.send must be called exactly once"
    assert len(fallback_calls) == 1, "fallback_sender.send must be called exactly once"
    assert primary_calls[0] == "test-critical-message"
    assert fallback_calls[0] == "test-critical-message"


@pytest.mark.asyncio
async def test_send_critical_without_fallback_only_calls_primary() -> None:
    """_send_critical must work with no fallback sender configured."""
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    primary_calls: list[str] = []

    async def primary_send(msg: str, *, critical: bool = False) -> None:
        primary_calls.append(msg)

    primary = mock.AsyncMock()
    primary.send = primary_send

    dispatcher = NotificationDispatcher(sender=primary, fallback_sender=None)
    await dispatcher._send_critical("only-primary-message")

    assert len(primary_calls) == 1
    assert primary_calls[0] == "only-primary-message"


@pytest.mark.asyncio
async def test_send_critical_uses_asyncio_gather_in_source() -> None:
    """_send_critical implementation must use asyncio.gather for parallel dispatch."""
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    source = inspect.getsource(NotificationDispatcher._send_critical)
    assert "asyncio.gather" in source, "_send_critical must use asyncio.gather"


@pytest.mark.asyncio
async def test_send_critical_fallback_exception_does_not_raise() -> None:
    """_send_critical must not propagate exceptions from fallback sender."""
    from hft_platform.notifications.dispatcher import NotificationDispatcher

    async def primary_send(msg: str, *, critical: bool = False) -> None:
        pass

    async def failing_fallback_send(msg: str) -> None:
        raise RuntimeError("fallback network error")

    primary = mock.AsyncMock()
    primary.send = primary_send
    fallback = mock.AsyncMock()
    fallback.send = failing_fallback_send

    dispatcher = NotificationDispatcher(sender=primary, fallback_sender=fallback)
    # Should not raise — gather uses return_exceptions=True
    await dispatcher._send_critical("message")


# ---------------------------------------------------------------------------
# T6: templates.py — heartbeat pnl_scaled displays NTD with // 10000
# ---------------------------------------------------------------------------


def test_render_heartbeat_pnl_displays_ntd_conversion() -> None:
    """render_heartbeat must divide pnl_scaled by 10000 and show NTD unit."""
    from hft_platform.notifications.templates import render_heartbeat

    result = render_heartbeat(
        autonomy_state="ACTIVE",
        pnl_scaled=50000,
        strategies_active=2,
        feed_status="ok",
    )
    assert "5 NTD" in result, f"Expected '5 NTD' in heartbeat output, got: {result!r}"
    assert "NTD" in result


def test_render_heartbeat_pnl_negative_scaled() -> None:
    """render_heartbeat must correctly handle negative pnl_scaled values."""
    from hft_platform.notifications.templates import render_heartbeat

    result = render_heartbeat(
        autonomy_state="ACTIVE",
        pnl_scaled=-30000,
        strategies_active=1,
        feed_status="ok",
    )
    assert "-3 NTD" in result, f"Expected '-3 NTD' in heartbeat output, got: {result!r}"


def test_render_heartbeat_pnl_zero() -> None:
    """render_heartbeat must display 0 NTD when pnl_scaled is zero."""
    from hft_platform.notifications.templates import render_heartbeat

    result = render_heartbeat(
        autonomy_state="ACTIVE",
        pnl_scaled=0,
        strategies_active=0,
        feed_status="disconnected",
    )
    assert "0 NTD" in result, f"Expected '0 NTD' in heartbeat output, got: {result!r}"


def test_render_heartbeat_pnl_not_raw_scaled_value() -> None:
    """render_heartbeat must not emit the raw scaled integer (e.g. 50000)."""
    from hft_platform.notifications.templates import render_heartbeat

    result = render_heartbeat(
        autonomy_state="ACTIVE",
        pnl_scaled=50000,
        strategies_active=2,
        feed_status="ok",
    )
    # The raw scaled value (50000) must not appear in the output
    assert "50000" not in result, f"Raw pnl_scaled value must not appear in output, got: {result!r}"


# ---------------------------------------------------------------------------
# T7: alertmanager_bridge.py — Content-Length cap constant exists
# ---------------------------------------------------------------------------


def test_alertmanager_bridge_max_body_constant_is_one_mb() -> None:
    """The _MAX_BODY cap in _handle_connection must be exactly 1 MB (1_048_576)."""
    import hft_platform.notifications.alertmanager_bridge as bridge_module

    source = inspect.getsource(bridge_module.AlertmanagerBridge._handle_connection)
    assert "1_048_576" in source or "1048576" in source, "_handle_connection must define a 1 MB body cap constant"


def test_alertmanager_bridge_returns_413_on_oversized_content_length() -> None:
    """_handle_connection must return 413 when Content-Length > 1 MB."""
    from hft_platform.notifications.alertmanager_bridge import AlertmanagerBridge

    bridge = AlertmanagerBridge(port=0, sender=mock.MagicMock())

    written_data: list[bytes] = []

    class FakeWriter:
        def write(self, data: bytes) -> None:
            written_data.append(data)

        async def wait_closed(self) -> None:
            pass

        def close(self) -> None:
            pass

    oversized = 1_048_577  # 1 byte over the 1 MB cap
    request = (f"POST /webhook/alertmanager HTTP/1.1\r\nContent-Length: {oversized}\r\n\r\n").encode("utf-8")

    async def run() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(request)
        reader.feed_eof()
        await bridge._handle_connection(reader, FakeWriter())  # type: ignore[arg-type]

    asyncio.run(run())

    combined = b"".join(written_data)
    assert b"413" in combined, f"Expected 413 response for oversized payload, got: {combined!r}"


# ---------------------------------------------------------------------------
# T9: latency.py — Integer division latency_ns // 1000
# ---------------------------------------------------------------------------


def test_latency_recorder_uses_integer_division_for_microseconds(monkeypatch) -> None:
    """latency_us in the payload must be latency_ns // 1000 (integer, truncated)."""
    from hft_platform.observability.latency import LatencyRecorder

    monkeypatch.setenv("HFT_LATENCY_TRACE", "1")
    monkeypatch.setenv("HFT_LATENCY_SAMPLE_EVERY", "1")
    LatencyRecorder.reset_for_tests()
    rec = LatencyRecorder.get()
    queue: asyncio.Queue = asyncio.Queue()
    rec.configure(queue)

    # 5500 ns // 1000 = 5 (integer division truncates, not rounds)
    rec.record("normalize", 5500, ts_ns=100_000_000)

    item = queue.get_nowait()
    latency_us = item["data"]["latency_us"]
    assert latency_us == 5, f"Expected latency_us=5 (integer division 5500//1000), got {latency_us!r}"
    assert isinstance(latency_us, int), f"latency_us must be int, not float; got {type(latency_us)}"


def test_latency_recorder_integer_division_does_not_round(monkeypatch) -> None:
    """Integer division must truncate toward zero, not round (999 ns → 0 µs, not 1 µs)."""
    from hft_platform.observability.latency import LatencyRecorder

    monkeypatch.setenv("HFT_LATENCY_TRACE", "1")
    monkeypatch.setenv("HFT_LATENCY_SAMPLE_EVERY", "1")
    LatencyRecorder.reset_for_tests()
    rec = LatencyRecorder.get()
    queue: asyncio.Queue = asyncio.Queue()
    rec.configure(queue)

    rec.record("normalize", 999, ts_ns=100_000_000)

    item = queue.get_nowait()
    latency_us = item["data"]["latency_us"]
    assert latency_us == 0, f"999 ns // 1000 must yield 0 µs (truncation), got {latency_us!r}"


def test_latency_recorder_source_uses_floor_division() -> None:
    """The record() method source must use // 1000, not / 1000."""
    from hft_platform.observability.latency import LatencyRecorder

    source = inspect.getsource(LatencyRecorder.record)
    assert "// 1000" in source, "record() must use integer floor division (// 1000)"
    # Verify no float division idiom like `/ 1000`
    # We look for the specific assignment pattern
    assert "latency_ns / 1000" not in source, "record() must not use float division (/ 1000) for latency_us"


# ---------------------------------------------------------------------------
# T10: slippage.py — commission fallback uses max(0, fee_ntd - tax_ntd)
# ---------------------------------------------------------------------------


def test_slippage_decomposer_commission_is_zero_when_fee_equals_tax() -> None:
    """commission_bps must be 0.0 when fee_ntd == tax_ntd (not fee_ntd itself)."""
    from hft_platform.tca.slippage import SlippageDecomposer

    decomposer = SlippageDecomposer(point_value=10, tick_size=1.0)
    result = decomposer.decompose(
        decision_price=180_000_0000,  # 180,000 NTD (scaled x10000)
        arrival_price=180_000_0000,
        fill_price=180_000_0000,
        notional_ntd=100_000,
        fee_ntd=50,
        tax_ntd=50,  # fee == tax → commission should be 0
    )
    assert result.commission_bps == 0.0, f"When fee_ntd == tax_ntd, commission must be 0.0, got {result.commission_bps}"


def test_slippage_decomposer_commission_is_zero_when_fee_less_than_tax() -> None:
    """commission_bps must be 0.0 when fee_ntd < tax_ntd (guard against negative)."""
    from hft_platform.tca.slippage import SlippageDecomposer

    decomposer = SlippageDecomposer(point_value=10, tick_size=1.0)
    result = decomposer.decompose(
        decision_price=180_000_0000,
        arrival_price=180_000_0000,
        fill_price=180_000_0000,
        notional_ntd=100_000,
        fee_ntd=30,
        tax_ntd=50,  # fee < tax → commission must not be fee_ntd (30), must be 0
    )
    assert result.commission_bps == 0.0, f"When fee_ntd < tax_ntd, commission must be 0.0, got {result.commission_bps}"
    assert result.commission_bps >= 0.0, "commission_bps must never be negative"


def test_slippage_decomposer_commission_positive_when_fee_exceeds_tax() -> None:
    """commission_bps must be positive when fee_ntd > tax_ntd."""
    from hft_platform.tca.slippage import SlippageDecomposer

    decomposer = SlippageDecomposer(point_value=10, tick_size=1.0)
    result = decomposer.decompose(
        decision_price=180_000_0000,
        arrival_price=180_000_0000,
        fill_price=180_000_0000,
        notional_ntd=100_000,
        fee_ntd=100,
        tax_ntd=40,  # commission = 60 NTD
    )
    expected_commission_ntd = 60.0
    expected_commission_bps = (expected_commission_ntd / 100_000) * 10_000.0
    assert abs(result.commission_bps - expected_commission_bps) < 1e-9, (
        f"Expected commission_bps={expected_commission_bps}, got {result.commission_bps}"
    )


def test_slippage_decomposer_commission_uses_max_in_source() -> None:
    """SlippageDecomposer.decompose source must use max(0, fee_ntd - tax_ntd)."""
    from hft_platform.tca.slippage import SlippageDecomposer

    source = inspect.getsource(SlippageDecomposer.decompose)
    assert "max(0," in source or "max(0, " in source, (
        "decompose() must use max(0, fee_ntd - tax_ntd) to guard against negative commission"
    )
