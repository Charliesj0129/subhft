"""Sliding-window rate limiter with an injectable clock and sleep (tests never sleep for real)."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable


class RateLimiter:
    """At most ``max_calls`` acquisitions in any ``period_s`` window; ``acquire`` blocks until a slot is free."""

    def __init__(
        self,
        max_calls: int,
        period_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._max = max_calls
        self._period = period_s
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()

    def acquire(self) -> None:
        while True:
            now = self._clock()
            while self._calls and now - self._calls[0] >= self._period:
                self._calls.popleft()
            if len(self._calls) < self._max:
                self._calls.append(now)
                return
            self._sleep(self._period - (now - self._calls[0]))
