import asyncio
import collections
import os
import threading
import time
from typing import Any, List

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.engine.event_bus_telemetry import (
    NOOP_EVENT_BUS_TELEMETRY,
    ConsumerLagSink,
    EventBusTelemetry,
)
from hft_platform.events import GapEvent

logger = get_logger("event_bus")

# Bound once: _notify runs on every publish and compares the caller's thread with the loop thread.
_get_ident = threading.get_ident

_WAIT_MODE = os.getenv("HFT_BUS_WAIT_MODE", "event").lower()
_USE_TYPED_TICK_RING = os.getenv("HFT_BUS_TYPED_TICK_RING", "0").lower() not in {"0", "false", "no", "off"}
_USE_TYPED_BOOK_RINGS = os.getenv("HFT_BUS_TYPED_BOOK_RINGS", "0").lower() not in {"0", "false", "no", "off"}
_TYPED_BIDASK_PACKED_LEVELS = max(1, int(os.getenv("HFT_BUS_TYPED_BIDASK_PACKED_LEVELS", "5")))
# Release deadline of a bus whose consumed-slot release is off: never reached, so publish pays one int compare.
_NEVER_SEQ = 1 << 62


class _PyFastTickRingBuffer:
    def __init__(self, size: int) -> None:
        self.size = max(1, int(size))
        self.buffer: list[tuple[str, int, int, int, bool, bool, int] | None] = [None] * self.size

    def set_tick(
        self,
        idx: int,
        symbol: str,
        price: int,
        volume: int,
        total_volume: int,
        is_simtrade: bool,
        is_odd_lot: bool,
        exch_ts: int,
    ) -> None:
        # Skip redundant coercions — normalizer already guarantees correct types
        self.buffer[int(idx) % self.size] = (
            symbol,
            price,
            volume,
            total_volume,
            is_simtrade,
            is_odd_lot,
            exch_ts,
        )

    def get(self, idx: int) -> Any | None:
        frame = self.buffer[int(idx) % self.size]
        if frame is None:
            return None
        return ("tick",) + frame


class _PyFastBidAskRingBuffer:
    def __init__(self, size: int) -> None:
        self.size = max(1, int(size))
        self.buffer: list[tuple[Any, ...] | None] = [None] * self.size

    def set_bidask(
        self,
        idx: int,
        symbol: str,
        bids: Any,
        asks: Any,
        exch_ts: int,
        is_snapshot: bool,
        has_stats: bool,
        best_bid: int,
        best_ask: int,
        bid_depth: int,
        ask_depth: int,
        mid_price: float,
        spread: float,
        imbalance: float,
    ) -> None:
        if has_stats:
            self.buffer[int(idx) % self.size] = (
                "bidask",
                str(symbol),
                bids,
                asks,
                int(exch_ts),
                bool(is_snapshot),
                int(best_bid),
                int(best_ask),
                int(bid_depth),
                int(ask_depth),
                float(mid_price),
                float(spread),
                float(imbalance),
            )
        else:
            self.buffer[int(idx) % self.size] = (
                "bidask",
                str(symbol),
                bids,
                asks,
                int(exch_ts),
                bool(is_snapshot),
            )

    def set_bidask_packed(
        self,
        idx: int,
        symbol: str,
        bid_flat: Any,
        bid_rows: int,
        ask_flat: Any,
        ask_rows: int,
        exch_ts: int,
        is_snapshot: bool,
        has_stats: bool,
        best_bid: int,
        best_ask: int,
        bid_depth: int,
        ask_depth: int,
        mid_price: float,
        spread: float,
        imbalance: float,
    ) -> None:
        self.buffer[int(idx) % self.size] = (
            "bidask_packed",
            str(symbol),
            tuple(int(x) for x in bid_flat),
            int(bid_rows),
            tuple(int(x) for x in ask_flat),
            int(ask_rows),
            int(exch_ts),
            bool(is_snapshot),
            bool(has_stats),
            int(best_bid),
            int(best_ask),
            int(bid_depth),
            int(ask_depth),
            float(mid_price),
            float(spread),
            float(imbalance),
        )

    def get(self, idx: int) -> Any | None:
        frame = self.buffer[int(idx) % self.size]
        if not frame:
            return frame
        if frame[0] != "bidask_packed":
            return frame
        (
            _,
            symbol,
            bid_flat,
            bid_rows,
            ask_flat,
            ask_rows,
            exch_ts,
            is_snapshot,
            has_stats,
            best_bid,
            best_ask,
            bid_depth,
            ask_depth,
            mid_price,
            spread,
            imbalance,
        ) = frame
        bids = [[int(bid_flat[i * 2]), int(bid_flat[i * 2 + 1])] for i in range(int(bid_rows))]
        asks = [[int(ask_flat[i * 2]), int(ask_flat[i * 2 + 1])] for i in range(int(ask_rows))]
        if has_stats:
            return (
                "bidask",
                symbol,
                bids,
                asks,
                exch_ts,
                is_snapshot,
                best_bid,
                best_ask,
                bid_depth,
                ask_depth,
                mid_price,
                spread,
                imbalance,
            )
        return ("bidask", symbol, bids, asks, exch_ts, is_snapshot)


def _pack_book_levels(levels: Any, max_levels: int = _TYPED_BIDASK_PACKED_LEVELS) -> tuple[tuple[int, ...], int] | None:
    if levels is None:
        return (), 0
    if isinstance(levels, (str, bytes)):
        return None
    try:
        flat: list[int] = []
        rows = 0
        for row in levels:
            if rows >= max_levels:
                break
            if row is None:
                continue
            try:
                price = int(row[0])
                vol = int(row[1])
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                return None
            flat.extend((price, vol))
            rows += 1
        return tuple(flat), rows
    except Exception as exc:
        logger.debug("operation_fallback", error=str(exc))
        return None


class _PyFastLOBStatsRingBuffer:
    def __init__(self, size: int) -> None:
        self.size = max(1, int(size))
        self.buffer: list[tuple[str, int, int, int, float, int, int, int, int] | None] = [None] * self.size

    def set_stats(
        self,
        idx: int,
        symbol: str,
        ts: int,
        mid_price_x2: int,
        spread_scaled: int,
        imbalance: float,
        best_bid: int,
        best_ask: int,
        bid_depth: int,
        ask_depth: int,
    ) -> None:
        self.buffer[int(idx) % self.size] = (
            str(symbol),
            int(ts),
            int(mid_price_x2),
            int(spread_scaled),
            float(imbalance),
            int(best_bid),
            int(best_ask),
            int(bid_depth),
            int(ask_depth),
        )

    def get(self, idx: int) -> Any | None:
        return self.buffer[int(idx) % self.size]


class RingBufferBus:
    """
    Improved RingBufferBus.
    Uses a single shared buffer (list) and cursors for consumers.
    This mimics the Disruptor pattern:
    - Single Writer (publish) -> writes to buffer[seq % size]
    - Multiple Readers -> track their own local_seq
    """

    def __init__(
        self,
        size: int = 65536,
        storm_guard: Any = None,
        *,
        telemetry: EventBusTelemetry | None = None,
    ) -> None:
        self.size = size
        self._use_typed_tick_ring = bool(_USE_TYPED_TICK_RING)
        self._use_typed_book_rings = bool(_USE_TYPED_BOOK_RINGS)
        self._tick_ring = _PyFastTickRingBuffer(size) if self._use_typed_tick_ring else None
        self._bidask_ring = _PyFastBidAskRingBuffer(size) if self._use_typed_book_rings else None
        self._lobstats_ring = _PyFastLOBStatsRingBuffer(size) if self._use_typed_book_rings else None
        self._kind_ring: list[int] | None = (
            [0] * size if (self._use_typed_tick_ring or self._use_typed_book_rings) else None
        )
        self.buffer = [None] * size
        self.cursor: int = -1  # Writing cursor
        self.single_writer = os.getenv("HFT_BUS_SINGLE_WRITER", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        # asyncio.Lock only orders coroutines of ONE loop; it cannot stop a producer thread. Every
        # multi-writer publish path therefore also takes this thread lock (see _publish_serialized).
        self.write_lock = asyncio.Lock()
        self._publish_lock = threading.Lock()
        # (event loop, its thread id) in ONE reference, so a producer thread never sees a loop with
        # another loop's thread id. Set when a consumer starts (or a coroutine publishes) on the loop.
        self._loop_binding: tuple[asyncio.AbstractEventLoop, int] | None = None
        self._bind_running_loop()
        self._telemetry = telemetry if telemetry is not None else NOOP_EVENT_BUS_TELEMETRY
        # Event for lock-free notification (optional)
        # Per-consumer signals to avoid lost-wakeup race (DEC-02)
        self.signal = None if _WAIT_MODE == "spin" else asyncio.Event()
        self._consumer_signals: list[asyncio.Event] = []
        self._consumer_positions: dict[str, int] = {}
        # Consumed-slot release: without it the ring pins the last `size` events, and every GC-tracked object they
        # own, in the old generations long after all consumers read them (each gen2 pass rescans them all).
        self._release_consumed_enabled = os.getenv("HFT_BUS_RELEASE_CONSUMED", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self._release_every = max(1, int(os.getenv("HFT_BUS_RELEASE_EVERY", "256")))
        # Most slots one release clears. A consumer that catches up a backlog of k events would otherwise free all k
        # (each owning arrays) inside a single publish; the rest follows on the next publishes. At least 2, so a
        # backlog drains faster than one publish adds to it.
        self._release_max_slots = max(2, int(os.getenv("HFT_BUS_RELEASE_MAX_SLOTS", str(2 * self._release_every))))
        # Highest seq whose slot was set to None by release (-1: none yet); read by consumers to detect a gap.
        # Published BEFORE the slots are cleared, so a consumer that reads a cleared slot also sees the watermark.
        self._released_upto: int = -1
        self._release_due_seq: int = self._release_every - 1 if self._release_consumed_enabled else _NEVER_SEQ
        # Release pins: the seq each consumer NAME has fully consumed. Unlike _consumer_positions they survive the
        # generator closing, so a consumer that crashed and restarts finds the events it never processed still in the
        # ring (a dead consumer holds the release exactly as the unreleased ring held them). Only forget_consumer() or
        # the consumer itself moves a pin.
        self._release_pins: dict[str, int] = {}
        # Orders every pin insert, move and removal against the release's min() over the pins; held for a few
        # instructions, never across an await, never while slots are being cleared.
        self._release_lock = threading.Lock()
        self._notify_every = max(1, int(os.getenv("HFT_BUS_NOTIFY_EVERY", "1")))
        self._notify_counter = 0
        self._spin_sleep = float(os.getenv("HFT_BUS_SPIN_SLEEP", "0"))
        self._spin_budget = max(1, int(os.getenv("HFT_BUS_SPIN_BUDGET", "100")))
        # StormGuard reference for triggering HALT on critical overflow
        self._storm_guard = storm_guard
        # Overflow threshold for triggering HALT (consecutive overflows)
        self._overflow_halt_threshold = int(os.getenv("HFT_BUS_OVERFLOW_HALT_THRESHOLD", "3"))
        self._overflow_count = 0  # legacy global (kept for backward compat metrics)
        self._overflow_count_per_consumer: dict[str, int] = {}  # per-consumer overflow tracking
        # Sliding-window overflow rate tracker (XB-03)
        self._overflow_window_s: float = float(os.getenv("HFT_BUS_OVERFLOW_WINDOW_S", "60.0"))
        self._overflow_rate_threshold: int = int(os.getenv("HFT_BUS_OVERFLOW_RATE_THRESHOLD", "10"))
        self._overflow_timestamps: collections.deque[float] = collections.deque(
            maxlen=self._overflow_rate_threshold * 2,
        )

    def set_storm_guard(self, storm_guard: Any) -> None:
        """Set StormGuard reference for overflow HALT triggering."""
        self._storm_guard = storm_guard

    def _record_overflow_rate(self) -> None:
        """Record an overflow timestamp and check sliding-window rate threshold (XB-03)."""
        now = time.monotonic()
        self._overflow_timestamps.append(now)
        # Evict timestamps older than the window
        cutoff = now - self._overflow_window_s
        while self._overflow_timestamps and self._overflow_timestamps[0] < cutoff:
            self._overflow_timestamps.popleft()
        if len(self._overflow_timestamps) >= self._overflow_rate_threshold and self._storm_guard is not None:
            rate_count = len(self._overflow_timestamps)
            self._storm_guard.trigger_halt(f"EventBus overflow rate: {rate_count} in {self._overflow_window_s}s")
            logger.critical(
                "StormGuard HALT triggered due to EventBus overflow rate",
                overflow_rate=rate_count,
                window_s=self._overflow_window_s,
                threshold=self._overflow_rate_threshold,
            )

    @property
    def released_upto(self) -> int:
        """Highest seq whose slot was released (set to None) after every registered consumer read it; -1 if none."""
        return self._released_upto

    def _register_consumer(self, consumer_name: str, local_seq: int) -> int:
        """Pin the ring for this consumer before it first waits; return the seq it starts reading after.

        A consumer asking to start behind the release watermark whose own pin is at or past the watermark
        already consumed everything released below it (it crashed and restarted): it resumes at the watermark.
        """
        if not self._release_consumed_enabled:
            return local_seq
        with self._release_lock:
            released = self._released_upto
            pin = self._release_pins.get(consumer_name)
            start = released if local_seq < released and pin is not None and pin >= released else local_seq
            self._release_pins[consumer_name] = start
        if start != local_seq:
            logger.info(
                "bus_consumer_rejoined_after_release",
                consumer=consumer_name,
                requested_start=local_seq,
                start=start,
            )
        return start

    def reserve_consumer(self, consumer_name: str, start_seq: int) -> bool:
        """Pin the ring at `start_seq` for a consumer that registers later; True if a pin was inserted.

        `start_seq` is the position the consumer will pass to consume() as `start_cursor`. Without a pin the
        release would run past it while it is still starting, and it would join behind the watermark and lose
        the events published in between. An existing pin is never moved: it records what that name really
        consumed, so lowering it could put the pin under the watermark (the release would stall) and raising
        it could release events the consumer has not read. A no-op, returning False, when the release is off.

        The caller must start the consumer afterwards, or call forget_consumer(): a reserved consumer that
        never runs holds the release exactly as the unreleased ring held its events.
        """
        if not self._release_consumed_enabled:
            return False
        with self._release_lock:
            if consumer_name in self._release_pins:
                return False
            released = self._released_upto
            self._release_pins[consumer_name] = start_seq
        if start_seq < released:
            logger.warning(
                "bus_consumer_reserved_behind_release",
                consumer=consumer_name,
                start_seq=start_seq,
                released_upto=released,
            )
        return True

    def forget_consumer(self, consumer_name: str) -> None:
        """Drop a consumer's release pin for good, so the release no longer waits for it.

        For a consumer that is gone and will not restart; a closed consumer that may restart keeps its pin.
        """
        with self._release_lock:
            self._release_pins.pop(consumer_name, None)

    def _advance_pin(self, consumer_name: str, seq: int) -> None:
        """Move a consumer's existing pin to `seq`; a no-op for a name with no pin (forgotten, or release off).

        The membership check and the store are one step under the release lock: apart, a forget_consumer() in
        between would be undone, and on a free-threaded build the insert could resize the dict while the
        producer iterates it in min(). Runs once per catch-up, not per event.
        """
        if not self._release_consumed_enabled:
            return
        with self._release_lock:
            if consumer_name in self._release_pins:
                self._release_pins[consumer_name] = seq

    def _release_when_due(self) -> None:
        """Run the release once the cursor has reached its deadline; called after the publish wakeup."""
        if self.single_writer:
            self._release_consumed(self.cursor)
            return
        # Several producer threads: a slot cleared here must not be one a producer has just overwritten a lap on,
        # so the release runs under the same lock as the cursor update.
        with self._publish_lock:
            self._release_consumed(self.cursor)

    def _release_consumed(self, cursor: int) -> None:
        """Drop up to `_release_max_slots` slots every pinned consumer has already read, so refcounting frees them.

        Due every `_release_every` publishes, and again on the very next publish while a backlog is left: a
        consumer that catches up k events costs ceil(k / max_slots) bounded releases, not one k-slot pause.
        The backlog cannot outlast the ring: each release shrinks (cursor - released) by max_slots - 1 net of
        the one event a publish adds, and max_slots >= 2. With no pinned consumer nothing is released.
        """
        with self._release_lock:
            pins = self._release_pins
            released = self._released_upto
            # A pinned consumer's pin only moves forward while it runs, so a value read just before it advances
            # is lower than the truth: releasing less is safe, releasing more is impossible. A pin is inserted,
            # moved or removed under this lock, so a new consumer can never be passed by a release in flight.
            target = min(pins.values()) if pins else released
            self._release_due_seq = cursor + self._release_every
            if target > cursor:
                target = cursor
            if target <= released:
                return
            # Only seqs newer than cursor - size still own their slot; an older one was overwritten.
            first = max(released + 1, cursor - self.size + 1)
            count = target - first + 1
            if count > self._release_max_slots:
                # The watermark advances only as far as the slots actually cleared below.
                count = self._release_max_slots
                target = first + count - 1
                self._release_due_seq = cursor + 1
            # Published BEFORE the slots are cleared, so a consumer that reads a cleared slot also sees it.
            self._released_upto = target
        # Outside the lock: a consumer or a registration never waits for the events to be freed. Nothing else
        # clears slots: one producer thread (single writer) or the publish lock (several) serializes releases.
        if count > 0:
            self._clear_slots(first, count)

    def _clear_slots(self, first_seq: int, count: int) -> None:
        """Set `count` consecutive ring slots, starting at `first_seq`'s, to None (count <= size)."""
        buf = self.buffer
        start = first_seq % self.size
        head = self.size - start
        if count <= head:
            buf[start : start + count] = [None] * count
        else:
            buf[start:] = [None] * head
            buf[: count - head] = [None] * (count - head)

    def _gap_floor(self, current_cursor: int) -> int:
        """Highest seq a consumer can no longer read: overwritten by the ring, or already released."""
        floor = current_cursor - self.size
        released = self._released_upto
        if released > floor and released >= 0:
            return released
        return floor

    def _account_overflow(self, consumer_name: str, lag: int, batch: bool) -> None:
        """Count one lapped-consumer overflow: counters, rate window, log and the StormGuard HALT thresholds."""
        self._telemetry.overflow_counter.inc()
        self._overflow_count += 1  # legacy global
        _consumer_ov = self._overflow_count_per_consumer.get(consumer_name, 0) + 1
        self._overflow_count_per_consumer[consumer_name] = _consumer_ov
        self._record_overflow_rate()
        logger.error(
            (
                "CRITICAL: Consumer batch lagged too much, data loss occurred"
                if batch
                else "CRITICAL: Consumer lagged too much, data loss occurred"
            ),
            consumer=consumer_name,
            lag=lag,
            overflow_count=_consumer_ov,
            threshold=self._overflow_halt_threshold,
        )

        # Trigger StormGuard HALT on repeated overflows (per-consumer)
        if _consumer_ov >= self._overflow_halt_threshold and self._storm_guard is not None:
            if batch:
                halt_msg = f"EventBus batch overflow: consumer={consumer_name}, {_consumer_ov} overflows, lag={lag}"
                self._storm_guard.trigger_halt(halt_msg)
                logger.critical("StormGuard HALT triggered due to EventBus batch overflow", consumer=consumer_name)
            else:
                halt_msg = f"EventBus overflow: consumer={consumer_name}, {_consumer_ov} overflows, lag={lag}"
                self._storm_guard.trigger_halt(halt_msg)
                logger.critical("StormGuard HALT triggered due to EventBus overflow", consumer=consumer_name)

    def _record_consumer_gap(
        self,
        consumer_name: str,
        local_seq: int,
        floor: int,
        current_cursor: int,
        lag_gauge: ConsumerLagSink,
        batch: bool,
        late_join: bool,
    ) -> GapEvent:
        """Account seqs (local_seq, floor] a consumer lost and return the GapEvent that reports them.

        A late join (the consumer asked to start behind the release watermark, not lapped by the ring) is
        reported by the GapEvent and gap counter only: it did not lag, so it is no overflow and no HALT input.
        """
        if late_join:
            logger.warning(
                "bus_consumer_joined_behind_release",
                consumer=consumer_name,
                requested_start=local_seq,
                released_upto=floor,
                missed_count=floor - local_seq,
            )
        else:
            self._account_overflow(consumer_name, current_cursor - local_seq, batch)

        # Inject GapEvent so downstream strategies can reset stale state
        self._consumer_positions[consumer_name] = floor
        self._advance_pin(consumer_name, floor)
        lag_gauge.set(self.cursor - floor)
        self._telemetry.gap_event_counter.inc()
        return GapEvent(
            missed_count=floor - local_seq,
            first_missed_seq=local_seq + 1,
            last_missed_seq=floor,
            ts=timebase.now_ns(),
        )

    def _publish_unlocked(self, event: Any) -> None:
        next_seq = self.cursor + 1

        handled_by_typed_ring = False
        if (
            self._use_typed_tick_ring
            and self._tick_ring is not None
            and isinstance(event, tuple)
            and len(event) >= 8
            and event[0] == "tick"
        ):
            try:
                _, symbol, price, volume, total_volume, is_simtrade, is_odd_lot, exch_ts = event[:8]
                self._tick_ring.set_tick(
                    next_seq,
                    str(symbol),
                    int(price),
                    int(volume),
                    int(total_volume),
                    bool(is_simtrade),
                    bool(is_odd_lot),
                    int(exch_ts),
                )
                if self._kind_ring is not None:
                    self._kind_ring[next_seq % self.size] = 1
                handled_by_typed_ring = True
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                handled_by_typed_ring = False
        elif (
            self._use_typed_book_rings
            and self._bidask_ring is not None
            and isinstance(event, tuple)
            and len(event) >= 6
            and event[0] == "bidask"
        ):
            try:
                _, symbol, bids, asks, exch_ts, is_snapshot, *rest = event
                if len(rest) >= 7:
                    best_bid, best_ask, bid_depth, ask_depth, mid_price, spread, imbalance = rest[:7]
                    has_stats = True
                else:
                    best_bid = best_ask = bid_depth = ask_depth = 0
                    mid_price = spread = imbalance = 0.0
                    has_stats = False
                packed_writer = getattr(self._bidask_ring, "set_bidask_packed", None)
                used_packed = False
                if callable(packed_writer):
                    packed_bids = _pack_book_levels(bids)
                    packed_asks = _pack_book_levels(asks)
                    if packed_bids is not None and packed_asks is not None:
                        bid_flat, bid_rows = packed_bids
                        ask_flat, ask_rows = packed_asks
                        packed_writer(
                            next_seq,
                            str(symbol),
                            bid_flat,
                            int(bid_rows),
                            ask_flat,
                            int(ask_rows),
                            int(exch_ts),
                            bool(is_snapshot),
                            bool(has_stats),
                            int(best_bid),
                            int(best_ask),
                            int(bid_depth),
                            int(ask_depth),
                            float(mid_price),
                            float(spread),
                            float(imbalance),
                        )
                        used_packed = True
                if not used_packed:
                    self._bidask_ring.set_bidask(
                        next_seq,
                        str(symbol),
                        bids,
                        asks,
                        int(exch_ts),
                        bool(is_snapshot),
                        bool(has_stats),
                        int(best_bid),
                        int(best_ask),
                        int(bid_depth),
                        int(ask_depth),
                        float(mid_price),
                        float(spread),
                        float(imbalance),
                    )
                if self._kind_ring is not None:
                    self._kind_ring[next_seq % self.size] = 2
                handled_by_typed_ring = True
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                handled_by_typed_ring = False
        elif (
            self._use_typed_book_rings
            and self._lobstats_ring is not None
            and isinstance(event, tuple)
            and len(event) == 10
            and isinstance(event[0], str)
        ):
            try:
                _tag, symbol, ts, mid_x2, spread, imbalance, best_bid, best_ask, bid_depth, ask_depth = event
                self._lobstats_ring.set_stats(
                    next_seq,
                    symbol,
                    int(ts),
                    int(mid_x2),
                    int(spread),
                    float(imbalance),
                    int(best_bid),
                    int(best_ask),
                    int(bid_depth),
                    int(ask_depth),
                )
                if self._kind_ring is not None:
                    self._kind_ring[next_seq % self.size] = 3
                handled_by_typed_ring = True
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                handled_by_typed_ring = False

        if not handled_by_typed_ring:
            if self._kind_ring is not None:
                self._kind_ring[next_seq % self.size] = 0
            self.buffer[next_seq % self.size] = event
        self.cursor = next_seq
        self._notify_counter += 1

    def _bind_running_loop(self) -> None:
        """Remember the running loop and its thread; a no-op when called outside a running loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._loop_binding = (loop, _get_ident())

    def _set_signals(self) -> None:
        """Set every consumer signal. Must run on the loop thread: asyncio.Event is not thread-safe."""
        # Per-consumer signals (DEC-02: avoids lost-wakeup race)
        for sig in self._consumer_signals:
            sig.set()
        # Legacy shared signal for backward compatibility
        if self.signal is not None:
            self.signal.set()

    def _notify(self) -> None:
        if self._notify_counter % self._notify_every != 0:
            return
        binding = self._loop_binding
        if binding is not None and binding[1] != _get_ident():
            # Producer thread, not the loop thread: Event.set() would touch loop state from the
            # wrong thread (the loop would not wake, or the wake would be lost). Hand it over.
            try:
                binding[0].call_soon_threadsafe(self._set_signals)
            except RuntimeError as exc:
                # Loop already closed (shutdown): no consumer can still be waiting on it.
                logger.debug("event_bus_notify_loop_closed", error=str(exc))
            return
        # Loop thread (or no loop bound yet): set directly. Kept inline, not routed through
        # _set_signals, to spare a Python call per publish.
        for sig in self._consumer_signals:
            sig.set()
        if self.signal is not None:
            self.signal.set()

    def _publish_serialized(self, event: Any) -> None:
        """Publish under the thread lock: the cursor read-modify-write must not interleave."""
        with self._publish_lock:
            self._publish_unlocked(event)

    def publish_nowait(self, event: Any) -> None:
        """Synchronous publish; lock-free in single-writer mode (single producer thread).

        With `single_writer` off (HFT_BUS_SINGLE_WRITER=0) it is safe from several producer
        threads: the cursor update runs under `_publish_lock` and the wakeup is marshalled to the
        loop thread by `_notify`.
        """
        if self.single_writer:
            self._publish_unlocked(event)
        else:
            self._publish_serialized(event)
        self._notify()
        # After the wakeup: freeing consumed events never delays the one a consumer is waiting for.
        if self.cursor >= self._release_due_seq:
            self._release_when_due()

    def publish_many_nowait(self, events: List[Any]) -> None:
        """Synchronous publish for a batch; same threading contract as `publish_nowait`."""
        if self.single_writer:
            for event in events:
                self._publish_unlocked(event)
        else:
            with self._publish_lock:
                for event in events:
                    self._publish_unlocked(event)
        self._notify()
        if self.cursor >= self._release_due_seq:
            self._release_when_due()

    async def publish(self, event: Any):
        """Publish event to shared buffer."""
        if self._loop_binding is None:
            self._bind_running_loop()
        if self.single_writer:
            # Single-writer fast path: no lock
            self._publish_unlocked(event)
            self._notify()
            if self.cursor >= self._release_due_seq:
                self._release_when_due()
            return

        async with self.write_lock:
            # Check if we are overwriting unread data?
            # For simplicity in this non-blocking design, we overwrite.
            # Ideally we track min_reader_cursor to prevent overwrite if strict usage.
            # But for HFT, latest data > stalled consumer.
            # write_lock orders coroutines only; the thread lock also fences producer threads.
            self._publish_serialized(event)
            # _notify(), not a bare ``self.signal.set()``. ``consume()`` waits
            # on its OWN Event from ``_consumer_signals`` (DEC-02, added to fix
            # a lost-wakeup race); the shared ``self.signal`` is only kept for
            # backward compatibility and nothing awaits it. Setting just the
            # legacy signal left every consumer parked in ``my_signal.wait()``
            # forever -- a permanent hang, not a slowdown -- on the
            # multi-writer path.
            self._notify()
            if self.cursor >= self._release_due_seq:
                self._release_when_due()

    async def publish_many(self, events: List[Any]):
        """Publish a batch of events."""
        if self._loop_binding is None:
            self._bind_running_loop()
        if self.single_writer:
            for event in events:
                self._publish_unlocked(event)
            self._notify()
            if self.cursor >= self._release_due_seq:
                self._release_when_due()
            return

        async with self.write_lock:
            with self._publish_lock:
                for event in events:
                    self._publish_unlocked(event)
            # Same lost-wakeup as publish() above: per-consumer signals, not
            # the legacy shared one.
            self._notify()
            if self.cursor >= self._release_due_seq:
                self._release_when_due()

    async def consume(self, start_cursor: int | None = None, consumer_name: str = "unknown"):
        """Async generator for consuming events."""
        # If start_cursor is None, join at current (latest).
        # To replay from beginning, pass -1.
        local_seq = self.cursor if start_cursor is None else start_cursor
        # Per-consumer signal to avoid lost-wakeup race (DEC-02)
        my_signal: asyncio.Event | None = None
        if self.signal is not None:
            # Bind BEFORE registering: a producer thread that can see the signal must also see the
            # loop it has to marshal the wakeup to.
            self._bind_running_loop()
            my_signal = asyncio.Event()
            self._consumer_signals.append(my_signal)
        lag_gauge = self._telemetry.bind_consumer(consumer_name)

        try:
            # Before the first wait: a consumer that has not caught up yet must still hold back the release.
            local_seq = self._register_consumer(consumer_name, local_seq)
            joining = True
            while True:
                # Wait for data
                while self.cursor <= local_seq:
                    if my_signal is not None:
                        await my_signal.wait()
                        my_signal.clear()
                    else:
                        # Spin-wait mode: lock-free signaling via cursor polling.
                        if self._spin_sleep <= 0:
                            for _ in range(self._spin_budget):
                                if self.cursor > local_seq:
                                    break
                            if self.cursor <= local_seq:
                                await asyncio.sleep(0)
                        else:
                            await asyncio.sleep(self._spin_sleep)

                # Catch up batch
                current_cursor = self.cursor
                # Seqs up to the floor are gone: overwritten (consumer lagged a full ring, buffer wrap
                # protection) or released (consumer joined behind the release watermark). Skip to it, loudly.
                floor = self._gap_floor(current_cursor)
                late_join = joining and local_seq >= current_cursor - self.size
                joining = False
                if local_seq < floor:
                    gap_event = self._record_consumer_gap(
                        consumer_name, local_seq, floor, current_cursor, lag_gauge, False, late_join
                    )
                    local_seq = floor
                    yield gap_event

                interrupted = False
                while local_seq < current_cursor:
                    local_seq += 1
                    kind = self._kind_ring[local_seq % self.size] if self._kind_ring is not None else 0
                    if kind == 1 and self._tick_ring is not None:
                        event = self._tick_ring.get(local_seq)
                    elif kind == 2 and self._bidask_ring is not None:
                        event = self._bidask_ring.get(local_seq)
                    elif kind == 3 and self._lobstats_ring is not None:
                        raw = self._lobstats_ring.get(local_seq)
                        # Prepend "lobstats" tag so strategy dispatch matches
                        event = ("lobstats",) + raw if raw is not None else None
                    else:
                        event = self.buffer[local_seq % self.size]
                    if event is not None:
                        yield event
                    elif kind == 0 and local_seq <= self._released_upto:
                        # Released before this consumer read it (a second consumer registered under the same
                        # name advanced the shared position). Step back: the next catch-up reports a gap.
                        local_seq -= 1
                        interrupted = True
                        break

                # Track consumer position and report lag
                self._consumer_positions[consumer_name] = local_seq
                self._advance_pin(consumer_name, local_seq)
                lag_gauge.set(self.cursor - local_seq)
                if interrupted:
                    continue

                # Successful catch-up with no overflow — reset per-consumer counter
                if self._overflow_count_per_consumer.get(consumer_name, 0) > 0:
                    self._overflow_count_per_consumer[consumer_name] = 0
                if self._overflow_count > 0:
                    self._overflow_count = 0  # legacy global
        finally:
            # Unregister per-consumer signal on generator close
            if my_signal is not None and my_signal in self._consumer_signals:
                self._consumer_signals.remove(my_signal)
            self._consumer_positions.pop(consumer_name, None)
            self._overflow_count_per_consumer.pop(consumer_name, None)

    async def consume_batch(self, batch_size: int, start_cursor: int | None = None, consumer_name: str = "unknown"):
        """Async generator yielding lists of events."""
        batch_size = max(1, batch_size)
        local_seq = self.cursor if start_cursor is None else start_cursor
        # Per-consumer signal (DEC-02)
        my_signal: asyncio.Event | None = None
        if self.signal is not None:
            self._bind_running_loop()  # before registering; see consume()
            my_signal = asyncio.Event()
            self._consumer_signals.append(my_signal)
        lag_gauge = self._telemetry.bind_consumer(consumer_name)

        try:
            local_seq = self._register_consumer(consumer_name, local_seq)  # before the first wait; see consume()
            joining = True
            while True:
                while self.cursor <= local_seq:
                    if my_signal is not None:
                        await my_signal.wait()
                        my_signal.clear()
                    else:
                        if self._spin_sleep <= 0:
                            for _ in range(self._spin_budget):
                                if self.cursor > local_seq:
                                    break
                            if self.cursor <= local_seq:
                                await asyncio.sleep(0)
                        else:
                            await asyncio.sleep(self._spin_sleep)

                current_cursor = self.cursor
                floor = self._gap_floor(current_cursor)  # overwritten or released; see consume()
                late_join = joining and local_seq >= current_cursor - self.size
                joining = False
                if local_seq < floor:
                    gap_event = self._record_consumer_gap(
                        consumer_name, local_seq, floor, current_cursor, lag_gauge, True, late_join
                    )
                    local_seq = floor
                    # Inject GapEvent into batch so downstream strategies can reset stale state
                    yield [gap_event]

                batch: List[Any] = []
                interrupted = False
                while local_seq < current_cursor and len(batch) < batch_size:
                    local_seq += 1
                    kind = self._kind_ring[local_seq % self.size] if self._kind_ring is not None else 0
                    if kind == 1 and self._tick_ring is not None:
                        event = self._tick_ring.get(local_seq)
                    elif kind == 2 and self._bidask_ring is not None:
                        event = self._bidask_ring.get(local_seq)
                    elif kind == 3 and self._lobstats_ring is not None:
                        raw = self._lobstats_ring.get(local_seq)
                        # Prepend "lobstats" tag so strategy dispatch matches
                        event = ("lobstats",) + raw if raw is not None else None
                    else:
                        event = self.buffer[local_seq % self.size]
                    if event is not None:
                        batch.append(event)
                    elif kind == 0 and local_seq <= self._released_upto:
                        # Released before this consumer read it; see consume(). The events already in
                        # this batch go out first, then the next catch-up reports the gap.
                        local_seq -= 1
                        interrupted = True
                        break

                if batch:
                    yield batch

                # Track consumer position and report lag
                self._consumer_positions[consumer_name] = local_seq
                self._advance_pin(consumer_name, local_seq)
                lag_gauge.set(self.cursor - local_seq)
                if interrupted:
                    continue

                if self._overflow_count_per_consumer.get(consumer_name, 0) > 0:
                    self._overflow_count_per_consumer[consumer_name] = 0
                if self._overflow_count > 0:
                    self._overflow_count = 0
        finally:
            if my_signal is not None and my_signal in self._consumer_signals:
                self._consumer_signals.remove(my_signal)
            self._consumer_positions.pop(consumer_name, None)
            self._overflow_count_per_consumer.pop(consumer_name, None)
