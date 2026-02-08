from collections import deque

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("order.rate_limiter")


class RateLimiter:
    def __init__(self, soft_cap: int, hard_cap: int, window_s: int):
        self.rate_window: deque[float] = deque()
        self.soft_cap = soft_cap
        self.hard_cap = hard_cap
        self.window_s = window_s

    def update(self, soft_cap: int | None = None, hard_cap: int | None = None, window_s: int | None = None) -> None:
        if soft_cap is not None:
            self.soft_cap = soft_cap
        if hard_cap is not None:
            self.hard_cap = hard_cap
        if window_s is not None:
            self.window_s = window_s

    def check(self) -> bool:
        now = timebase.now_s()
        while self.rate_window and now - self.rate_window[0] > self.window_s:
            self.rate_window.popleft()

        if len(self.rate_window) >= self.hard_cap:
            logger.error("Hard Rate Limit Hit", count=len(self.rate_window))
            return False

        if len(self.rate_window) >= self.soft_cap:
            logger.warning("Soft Rate Limit Hit", count=len(self.rate_window))

        return True

    def record(self) -> None:
        self.rate_window.append(timebase.now_s())
