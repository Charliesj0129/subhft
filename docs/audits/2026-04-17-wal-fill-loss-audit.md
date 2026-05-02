# 2026-04-17 WAL / Recorder Fill-Loss Audit

**Scope**: `src/hft_platform/execution/router.py` fill-recording paths.
**Trigger**: 3-contract discrepancy between CK fills (net 0) and broker
state (-1) at end-of-day 2026-04-16. Hypothesis: fills lost when
`recorder_queue` was full and WAL fallback failed.

## Code path trace (router.py)

Fills flow through three recording sites, all with the same pattern:

1. **Live path** — `run()` loop, lines 428-440: after `on_fill` mutates
   the position, `map_event_to_record` produces `(topic, payload)`;
   `recorder_queue.put_nowait(...)` is attempted (L436). On
   `asyncio.QueueFull`, `recorder_exec_drops_total{topic=fills}` is
   incremented, a warning is logged, and `_wal_fallback_write` is
   invoked (L438-440).
2. **Shutdown drain** — `stop()`/drain loop, lines 553-567: same
   recorder→WAL fallback (L560-563). If mapping fails (`_mapped` is
   falsy), the raw `fill_event` is WAL-written directly (L567); no
   metric or error is incremented for unmappable fills in this branch.
3. **DLQ retry path** — lines 760-767: same structure for resolved
   orphaned fills.

`_wal_fallback_write` (L772-804):
- If `self._wal_writer is None`: logs `fill_data_loss` at
  **critical** and increments `exec_fill_data_loss_total`. Good.
- Otherwise schedules `asyncio.ensure_future(wal_writer.write(...))`
  with a done-callback `_on_wal_done` (L792-802) that logs
  `wal_fallback_async_failed` at **error** level and increments
  `recorder_exec_wal_fallback_failure_total{topic}` if the future
  raised.
- A synchronous exception from `ensure_future` itself falls into
  `except Exception as wal_exc` (L803-804), logging
  `wal_fallback_failed` at **error** — but **no metric** is
  incremented and **no fill-loss counter** is touched.

## Identified failure modes

1. **Silent loss when WAL write raises synchronously** (L803-804):
   scheduling failure is logged but `exec_fill_data_loss_total` is not
   incremented. An operator watching only the loss counter would miss
   this.
2. **Silent loss when WAL write fails asynchronously** (L792-802):
   the done-callback increments a "fallback failure" counter, but the
   fill is now completely gone from CK and WAL; the primary loss
   counter is not incremented. Alert rules keyed on
   `exec_fill_data_loss_total` will be blind.
3. **Disk-full / rotate-window**: `WalWriter.write` presumably raises
   `OSError` inside the future; caught by the done-callback → case 2.
4. **Unmappable fill during shutdown drain** (L564-565): logged
   `shutdown_drain_fill_unmappable` at warning only; no metric and no
   WAL attempt. Fills mapped from unknown symbols silently drop.
5. **Fire-and-forget WAL task** lifetime is not tracked by any
   supervisor — a pending `ensure_future` can be dropped if the event
   loop shuts down before the I/O completes.

## Recommended hardening (do not implement yet)

- Increment `exec_fill_data_loss_total` in both the synchronous
  `except` branch (L803) and the async failure callback (L797) so the
  single canonical loss metric captures every permanent loss.
- Raise log level of the async WAL failure from `error` to
  `critical` (operator-waking) and include `fill_id`/`symbol`.
- For unmappable shutdown-drain fills (L564-565), call
  `_wal_fallback_write("fills_unmappable", fill_event)` instead of
  warn-and-drop; add a dedicated metric.
- Track outstanding WAL futures in a set and `await` them during
  `stop()` so the loop cannot exit with pending I/O.
- Add an end-of-day reconciliation alert: compare
  `sum(fill_event.qty)` from position-store telemetry vs CK fills
  count; any drift > 0 fires before midnight.
- Pre-allocate WAL headroom / rotate on size, not on clock, to close
  the rotate-window race.
