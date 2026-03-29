import enum
import os
from collections import deque

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("core.rate_limiter")


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


class PerSymbolRateResult(enum.Enum):
    OK = "ok"
    SOFT = "soft"
    HARD = "hard"


class PerSymbolRateLimiter:
    """Per-symbol sliding window rate limiter with cardinality bounds."""

    __slots__ = (
        "_windows",
        "_soft_limit",
        "_hard_limit",
        "_window_s",
        "_call_count",
        "_max_symbols",
    )

    def __init__(
        self,
        soft_limit: int | None = None,
        hard_limit: int | None = None,
        window_s: float | None = None,  # precision-ok: time
        max_symbols: int = 10_000,
    ):
        self._soft_limit = soft_limit or int(os.getenv("HFT_PER_SYMBOL_RATE_SOFT", "30"))
        self._hard_limit = hard_limit or int(os.getenv("HFT_PER_SYMBOL_RATE_HARD", "50"))
        self._window_s = window_s or float(os.getenv("HFT_PER_SYMBOL_RATE_WINDOW", "10"))  # precision-ok: time
        self._windows: dict[str, deque[float]] = {}
        self._call_count = 0
        self._max_symbols = max_symbols

    @property
    def soft_limit(self) -> int:
        return self._soft_limit

    @property
    def hard_limit(self) -> int:
        return self._hard_limit

    def check(self, symbol: str) -> PerSymbolRateResult:
        """Check rate for a symbol. Returns OK, SOFT, or HARD."""
        now = timebase.now_s()
        window = self._windows.get(symbol)
        if window is None:
            return PerSymbolRateResult.OK

        while window and now - window[0] > self._window_s:
            window.popleft()

        count = len(window)
        if count >= self._hard_limit:
            logger.error("Per-symbol hard rate limit", symbol=symbol, count=count)
            return PerSymbolRateResult.HARD

        if count >= self._soft_limit:
            logger.warning("Per-symbol soft rate limit", symbol=symbol, count=count)
            return PerSymbolRateResult.SOFT

        return PerSymbolRateResult.OK

    def record(self, symbol: str) -> None:
        """Record an order for a symbol."""
        self._call_count += 1

        if self._call_count % 100 == 0:
            self._evict_idle()

        if symbol not in self._windows and len(self._windows) >= self._max_symbols:
            logger.warning(
                "Per-symbol rate limiter cardinality limit",
                current=len(self._windows),
                max=self._max_symbols,
            )
            return

        if symbol not in self._windows:
            self._windows[symbol] = deque()
        self._windows[symbol].append(timebase.now_s())

    def _evict_idle(self) -> None:
        """Remove symbols with empty windows."""
        now = timebase.now_s()
        to_remove = []
        for sym, window in self._windows.items():
            while window and now - window[0] > self._window_s:
                window.popleft()
            if not window:
                to_remove.append(sym)
        for sym in to_remove:
            del self._windows[sym]
