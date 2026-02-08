import threading

from structlog import get_logger

from hft_platform.core import timebase

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
    def open_until(self) -> float:
        with self._lock:
            return self._open_until

    @open_until.setter
    def open_until(self, value: float) -> None:
        with self._lock:
            self._open_until = value

    def is_open(self) -> bool:
        with self._lock:
            return self._open_until > timebase.now_s()

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0

    def record_failure(self) -> bool:
        with self._lock:
            self._failure_count += 1
            if self._failure_count >= self.threshold:
                self._open_until = timebase.now_s() + self.timeout_s
                logger.critical("Circuit Breaker Tripped", failure_count=self._failure_count)
                return True
            return False
