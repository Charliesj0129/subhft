import time

from structlog import get_logger

logger = get_logger("order.circuit_breaker")


class CircuitBreaker:
    def __init__(self, threshold: int, timeout_s: int):
        self.threshold = threshold
        self.timeout_s = timeout_s
        self.failure_count = 0
        self.open_until = 0.0

    def is_open(self) -> bool:
        return self.open_until > time.time()

    def record_success(self) -> None:
        self.failure_count = 0

    def record_failure(self) -> bool:
        self.failure_count += 1
        if self.failure_count >= self.threshold:
            self.open_until = time.time() + self.timeout_s
            logger.critical("Circuit Breaker Tripped")
            return True
        return False
