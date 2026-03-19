"""WU-15: Fault Injection Framework.

Configurable failure injection for chaos-testing the HFT pipeline.
Hard-blocked in live mode.
"""

from __future__ import annotations

import asyncio
import os
import random
import threading

from structlog import get_logger

logger = get_logger("testing.fault_injector")


class FaultInjector:
    """Singleton fault injector with reproducible RNG."""

    __slots__ = (
        "_queue_drop_pct",
        "_latency_ms",
        "_broker_error_pct",
        "_feed_gap_s",
        "_rng",
    )

    _instance: FaultInjector | None = None
    _lock: threading.Lock = threading.Lock()

    def __init__(self) -> None:
        mode = os.getenv("HFT_MODE", "sim").strip().lower()
        if mode == "live":
            raise RuntimeError(
                "FaultInjector is hard-blocked in live mode (HFT_MODE=live). "
                "Set HFT_MODE to 'sim' or 'replay' for fault injection."
            )
        self._queue_drop_pct = float(
            os.getenv("HFT_FAULT_QUEUE_DROP_PCT", "0"),
        )
        self._latency_ms = float(os.getenv("HFT_FAULT_LATENCY_MS", "0"))
        self._broker_error_pct = float(
            os.getenv("HFT_FAULT_BROKER_ERROR_PCT", "0"),
        )
        self._feed_gap_s = float(os.getenv("HFT_FAULT_FEED_GAP_S", "0"))
        seed_env = os.getenv("HFT_FAULT_SEED")
        seed = int(seed_env) if seed_env is not None else None
        self._rng = random.Random(seed)
        logger.info(
            "fault_injector_init",
            queue_drop_pct=self._queue_drop_pct,
            latency_ms=self._latency_ms,
            broker_error_pct=self._broker_error_pct,
            feed_gap_s=self._feed_gap_s,
            seed=seed,
        )

    @classmethod
    def get(cls) -> FaultInjector:
        """Return the singleton instance (create on first call)."""
        if cls._instance is not None:
            return cls._instance
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (useful in tests)."""
        with cls._lock:
            cls._instance = None

    def should_drop_queue(self) -> bool:
        """Return True if the current event should be dropped."""
        if self._queue_drop_pct <= 0:
            return False
        return self._rng.random() * 100.0 < self._queue_drop_pct

    async def inject_latency(self) -> None:
        """Sleep for the configured latency (no-op if zero)."""
        if self._latency_ms <= 0:
            return
        await asyncio.sleep(self._latency_ms / 1000.0)

    def should_simulate_broker_error(self) -> bool:
        """Return True if a broker error should be simulated."""
        if self._broker_error_pct <= 0:
            return False
        return self._rng.random() * 100.0 < self._broker_error_pct

    async def inject_feed_gap(self) -> None:
        """Pause for configured feed-gap duration (no-op if zero)."""
        if self._feed_gap_s <= 0:
            return
        logger.warning("injecting_feed_gap", gap_s=self._feed_gap_s)
        await asyncio.sleep(self._feed_gap_s)

    @property
    def queue_drop_pct(self) -> float:
        return self._queue_drop_pct

    @property
    def latency_ms(self) -> float:
        return self._latency_ms

    @property
    def broker_error_pct(self) -> float:
        return self._broker_error_pct

    @property
    def feed_gap_s(self) -> float:
        return self._feed_gap_s
