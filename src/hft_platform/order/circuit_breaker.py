from __future__ import annotations

import os
import threading

from structlog import get_logger

import time

logger = get_logger("order.circuit_breaker")


class CircuitBreaker:
    """Thread-safe circuit breaker for order execution protection.

    All state modifications are protected by a lock to prevent race conditions
    in multi-threaded or multi-coroutine environments.
    """

    def __init__(self, threshold: int, timeout_s: int):
        self.threshold = threshold
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._failure_count = 0
        self._open_until = 0.0

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    @failure_count.setter
    def failure_count(self, value: int) -> None:
        with self._lock:
            self._failure_count = value

    @property
    def open_until(self) -> float:  # precision-time
        with self._lock:
            return self._open_until

    @open_until.setter
    def open_until(self, value: float) -> None:  # precision-time
        with self._lock:
            self._open_until = value

    def is_open(self) -> bool:
        with self._lock:
            return self._open_until > time.monotonic()

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0

    def record_failure(self) -> bool:
        with self._lock:
            self._failure_count += 1
            if self._failure_count >= self.threshold:
                self._open_until = time.monotonic() + self.timeout_s
                logger.critical("Circuit Breaker Tripped", failure_count=self._failure_count)
                return True
            return False


class StrategyCircuitBreakerManager:
    """Manages per-strategy circuit breakers with cardinality bounds."""

    __slots__ = (
        "_breakers",
        "_default_threshold",
        "_default_timeout_s",
        "_strategy_limits",
        "_max_strategies",
        "_lock",
    )

    def __init__(
        self,
        default_threshold: int | None = None,
        default_timeout_s: int | None = None,
        strategy_limits: dict | None = None,
        max_strategies: int = 10_000,
    ):
        self._default_threshold = default_threshold or int(os.getenv("HFT_STRATEGY_CB_THRESHOLD", "5"))
        self._default_timeout_s = default_timeout_s or int(os.getenv("HFT_STRATEGY_CB_TIMEOUT_S", "60"))
        self._strategy_limits = strategy_limits or {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._max_strategies = max_strategies
        self._lock = threading.Lock()

    def get_breaker(self, strategy_id: str) -> CircuitBreaker:
        """Get or create a circuit breaker for a strategy."""
        with self._lock:
            if strategy_id in self._breakers:
                return self._breakers[strategy_id]

            # Cardinality check
            if len(self._breakers) >= self._max_strategies:
                self._evict_idle()
                if len(self._breakers) >= self._max_strategies:
                    logger.warning(
                        "Strategy circuit breaker cardinality limit",
                        current=len(self._breakers),
                        max=self._max_strategies,
                    )
                    # Return a temporary open breaker
                    return CircuitBreaker(threshold=1, timeout_s=self._default_timeout_s)

            # Get per-strategy limits or defaults
            limits = self._strategy_limits.get(strategy_id, {})
            threshold = limits.get("cb_threshold", self._default_threshold)
            timeout_s = limits.get("cb_timeout_s", self._default_timeout_s)

            breaker = CircuitBreaker(threshold=threshold, timeout_s=timeout_s)
            self._breakers[strategy_id] = breaker
            return breaker

    def is_open(self, strategy_id: str) -> bool:
        """Check if a strategy's circuit breaker is open."""
        return self.get_breaker(strategy_id).is_open()

    def record_success(self, strategy_id: str) -> None:
        """Record a successful order for a strategy."""
        self.get_breaker(strategy_id).record_success()

    def record_failure(self, strategy_id: str) -> bool:
        """Record a failed order. Returns True if breaker tripped."""
        return self.get_breaker(strategy_id).record_failure()

    def _evict_idle(self) -> None:
        """Evict healthy breakers with zero failures."""
        to_remove = [sid for sid, b in self._breakers.items() if b.failure_count == 0 and not b.is_open()]
        for sid in to_remove:
            del self._breakers[sid]
