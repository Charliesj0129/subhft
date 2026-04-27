"""Cross-thread safety tests for AuditWriter._put (I-M2 fix).

`asyncio.Queue` is documented NOT thread-safe: ``put_nowait`` calls
``_wakeup_next(self._getters)`` which does ``waiter.set_result(None)`` and then
schedules wake via ``loop.call_soon`` (NOT ``call_soon_threadsafe``). Calling
``loop.call_soon`` from a non-loop thread is a documented thread-safety
violation that can corrupt the loop's ``_ready`` deque.

Production callers that hit this from a non-loop thread:
  * bootstrap lease-refresh daemon → storm_guard.trigger_halt
    → _emit_pending_transition → audit.log_guardrail_transition → _put
  * broker callback thread → market_data trigger_storm
    → _emit_pending_transition → audit q.put_nowait

Fix: ``_put`` detects current thread vs the engine loop thread; on a
non-loop thread it dispatches via ``loop.call_soon_threadsafe`` so the
actual queue mutation runs on the loop thread.

Tests in this module verify:
  1. Loop-thread path uses direct ``put_nowait`` (no call_soon_threadsafe).
  2. Non-loop-thread path uses ``call_soon_threadsafe`` (counter increments,
     data eventually appears in the queue).
  3. Concurrent loop + thread puts produce no exceptions and dequeue every
     item exactly once.
  4. Pre-start path (deque under lock) is unchanged.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import pytest

from hft_platform.recorder.audit import AuditWriter, reset_audit_writer


class _CapturingWriter:
    """Minimal mock writer that buffers all batches in memory.

    Replaces the flush-loop's downstream sink so test assertions can read
    rows after ``_flush_batch`` runs (instead of asserting on queue.qsize,
    which races with the flush loop draining items).
    """

    def __init__(self) -> None:
        self.batches: dict[str, list[dict]] = {}

    async def write(self, table: str, batch: list[dict]) -> None:
        self.batches.setdefault(table, []).extend(batch)


def _all_rows(writer: AuditWriter, table: str, sink: _CapturingWriter) -> list[dict]:
    """Collect all rows known to the writer: flushed (sink) + queue + overflow."""
    rows = list(sink.batches.get(table, []))
    q = writer._queues.get(table)
    if q is not None:
        # Drain non-destructively — copy via internal _queue deque.
        rows.extend(list(q._queue))  # type: ignore[attr-defined]
    rows.extend(list(writer._overflow.get(table, [])))
    return rows


class TestAuditWriterCrossThread:
    def setup_method(self) -> None:
        reset_audit_writer()

    def teardown_method(self) -> None:
        reset_audit_writer()

    @pytest.mark.asyncio
    async def test_put_from_loop_thread_uses_direct_path(self) -> None:
        """When _put is invoked on the engine loop thread the existing direct
        ``q.put_nowait(data)`` path is used (no call_soon_threadsafe overhead).
        """
        sink = _CapturingWriter()
        writer = AuditWriter(queue_size=100, writer=sink, flush_interval_ms=60_000)
        await writer.start()
        try:
            loop = asyncio.get_running_loop()
            with patch.object(
                loop, "call_soon_threadsafe", wraps=loop.call_soon_threadsafe
            ) as spy:
                writer.log_order({"cmd_id": 1})
            spy.assert_not_called()
            # Loop-thread fast path — no cross-thread bump.
            assert writer._cross_thread_count == 0
            # The flush loop will consume the item shortly; assert the row
            # eventually reaches the sink (or is still in queue/overflow).
            for _ in range(50):
                rows = _all_rows(writer, "audit.orders_log", sink)
                if len(rows) == 1:
                    break
                await asyncio.sleep(0.01)
            rows = _all_rows(writer, "audit.orders_log", sink)
            assert len(rows) == 1
            assert rows[0]["cmd_id"] == 1
        finally:
            await writer.stop()

    @pytest.mark.asyncio
    async def test_put_from_non_loop_thread_uses_call_soon_threadsafe(self) -> None:
        """When _put is invoked from a non-loop thread the fallback path
        schedules the queue mutation on the loop via call_soon_threadsafe and
        the cross-thread counter increments.
        """
        sink = _CapturingWriter()
        writer = AuditWriter(queue_size=100, writer=sink, flush_interval_ms=60_000)
        await writer.start()
        try:
            loop = asyncio.get_running_loop()
            cross_thread_before = writer._cross_thread_count

            with patch.object(
                loop, "call_soon_threadsafe", wraps=loop.call_soon_threadsafe
            ) as spy:
                done = threading.Event()

                def worker() -> None:
                    writer.log_guardrail_transition(
                        {"old_state": "NORMAL", "new_state": "HALT"}
                    )
                    done.set()

                t = threading.Thread(target=worker)
                t.start()
                t.join(timeout=2.0)
                assert done.is_set(), "worker thread did not finish"

                # Allow scheduled callback to run on the loop.
                for _ in range(50):
                    rows = _all_rows(writer, "audit.guardrail_log", sink)
                    if len(rows) == 1:
                        break
                    await asyncio.sleep(0.01)

            spy.assert_called()
            rows = _all_rows(writer, "audit.guardrail_log", sink)
            assert len(rows) == 1
            assert rows[0]["new_state"] == "HALT"
            assert writer._cross_thread_count == cross_thread_before + 1
        finally:
            await writer.stop()

    @pytest.mark.asyncio
    async def test_concurrent_loop_and_thread_puts_no_corruption(self) -> None:
        """Stress: loop coroutine + worker thread each put N rows. All 2N rows
        must end up in the queue/overflow with no exceptions raised.
        """
        N = 200
        sink = _CapturingWriter()
        writer = AuditWriter(queue_size=2 * N + 10, writer=sink, flush_interval_ms=60_000)
        await writer.start()
        try:
            loop = asyncio.get_running_loop()

            def worker() -> None:
                for i in range(N):
                    writer.log_order({"src": "thread", "i": i})

            t = threading.Thread(target=worker)
            t.start()

            # Concurrently drive the loop side.
            for i in range(N):
                writer.log_order({"src": "loop", "i": i})
                # Yield to the loop occasionally so call_soon_threadsafe
                # callbacks scheduled by the worker actually execute.
                if i % 32 == 0:
                    await asyncio.sleep(0)

            t.join(timeout=5.0)
            assert not t.is_alive(), "worker thread did not finish"

            # Drain all callbacks scheduled via call_soon_threadsafe.
            for _ in range(500):
                rows = _all_rows(writer, "audit.orders_log", sink)
                if len(rows) >= 2 * N:
                    break
                await asyncio.sleep(0.01)

            rows = _all_rows(writer, "audit.orders_log", sink)
            assert len(rows) == 2 * N, (
                f"expected {2 * N} rows enqueued, got {len(rows)}"
            )

            # P1-a (2026-04-27): _flush_batch now normalizes rows to canonical
            # DDL schema before handing to the writer. Pre-flush rows (still in
            # queue or overflow) keep their raw shape; post-flush rows (in
            # ``sink.batches``) have extras moved into the JSON ``details``
            # column. Resolve both forms to recover (src, i).
            import json as _json

            def _src_and_i(row: dict) -> tuple[str, int] | None:
                if "src" in row and "i" in row:
                    return row["src"], row["i"]
                details_blob = row.get("details", "")
                if not details_blob:
                    return None
                try:
                    parsed = _json.loads(details_blob)
                except (ValueError, TypeError):
                    return None
                if "src" in parsed and "i" in parsed:
                    return parsed["src"], parsed["i"]
                return None

            seen_loop: set[int] = set()
            seen_thread: set[int] = set()
            for row in rows:
                resolved = _src_and_i(row)
                assert resolved is not None, f"row missing (src, i): {row!r}"
                src, idx = resolved
                if src == "loop":
                    seen_loop.add(idx)
                else:
                    seen_thread.add(idx)

            assert seen_loop == set(range(N))
            assert seen_thread == set(range(N))
        finally:
            await writer.stop()

    def test_pre_start_buffer_unchanged(self) -> None:
        """Pre-start path does not touch the queue/loop machinery — _put still
        lands rows in the thread-safe deque under the pre-start lock.
        """
        writer = AuditWriter(queue_size=100)
        # No start() call — queues are not bound to any loop yet.
        writer.log_order({"cmd_id": 1})
        writer.log_guardrail_transition({"old_state": "NORMAL", "new_state": "WARM"})
        # Pre-start buffers contain the rows; no asyncio.Queue or loop has
        # been touched.
        assert len(writer._pre_start_buffer["audit.orders_log"]) == 1
        assert len(writer._pre_start_buffer["audit.guardrail_log"]) == 1
        # Cross-thread counter is untouched (only post-start path increments).
        assert writer._cross_thread_count == 0

    @pytest.mark.asyncio
    async def test_loop_thread_id_captured_in_start(self) -> None:
        """start() must capture both the engine loop reference and the loop
        thread id so subsequent _put calls can detect cross-thread context.
        """
        writer = AuditWriter(queue_size=10)
        await writer.start()
        try:
            assert writer._loop is asyncio.get_running_loop()
            assert writer._loop_thread_id == threading.get_ident()
        finally:
            await writer.stop()

    @pytest.mark.asyncio
    async def test_cross_thread_queue_full_routes_to_overflow(self) -> None:
        """When the queue is full, the cross-thread fallback must still
        respect the overflow buffer (not silently drop or raise).

        Uses a sink writer that BLOCKS on first write so the flush loop
        cannot drain the queue while the test is asserting state.
        """

        class _BlockingSink:
            def __init__(self) -> None:
                self.batches: list[list[dict]] = []
                self.unblock = asyncio.Event()

            async def write(self, table: str, batch: list[dict]) -> None:
                self.batches.append(list(batch))
                # Block forever so flush loop holds the items in batch and
                # the queue stays in its observable state for assertions.
                await self.unblock.wait()

        sink = _BlockingSink()
        writer = AuditWriter(queue_size=1, writer=sink, flush_interval_ms=60_000)
        await writer.start()
        try:
            # Fill queue from the loop thread; the flush loop will get the
            # row and block in sink.write — leaving subsequent puts to land
            # in the queue (size 1) until that overflows too.
            writer.log_order({"a": 1})
            # Wait until flush loop has consumed the row and is blocked in
            # sink.write.
            for _ in range(50):
                if len(sink.batches) == 1:
                    break
                await asyncio.sleep(0.01)
            assert len(sink.batches) == 1, "flush loop did not pull first row"

            # Push a second row from the LOOP thread so the queue has 1
            # item (queue size = 1, full).
            writer.log_order({"a": 2})
            # Now queue is full.
            assert writer._queues["audit.orders_log"].qsize() == 1

            done = threading.Event()

            def worker() -> None:
                writer.log_order({"a": 3})  # should land in overflow via fallback
                done.set()

            t = threading.Thread(target=worker)
            t.start()
            t.join(timeout=2.0)
            assert done.is_set()

            # Drain scheduled callback.
            for _ in range(50):
                if len(writer._overflow["audit.orders_log"]) == 1:
                    break
                await asyncio.sleep(0.01)

            assert writer._queues["audit.orders_log"].qsize() == 1
            assert len(writer._overflow["audit.orders_log"]) == 1
            # Unblock the sink so stop() can drain.
            sink.unblock.set()
        finally:
            await writer.stop()
