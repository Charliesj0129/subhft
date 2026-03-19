"""WU-14: Synthetic Tick Generator.

Configurable-rate TickEvent / BidAskEvent producer for load testing
and integration benchmarks.  All prices are scaled int (x10000),
timestamps sourced from ``timebase.now_ns()``.
"""

from __future__ import annotations

import argparse
import asyncio
import random
from typing import Any

import numpy as np
from structlog import get_logger

from hft_platform.core.timebase import now_ns
from hft_platform.events import BidAskEvent, MetaData, TickEvent

logger = get_logger("testing.load_generator")

_DEFAULT_PRICE = 600_0000
_DEFAULT_SPREAD = 1_0000
_DEFAULT_VOLUME = 1


class SyntheticTickGenerator:
    """Configurable synthetic market-data producer."""

    __slots__ = ("_symbol", "_base_price", "_spread", "_rng", "_seq")

    def __init__(
        self,
        symbol: str = "2330",
        base_price: int = _DEFAULT_PRICE,
        spread: int = _DEFAULT_SPREAD,
        seed: int | None = None,
    ) -> None:
        self._symbol = symbol
        self._base_price = base_price
        self._spread = spread
        self._rng = random.Random(seed)
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _make_meta(self) -> MetaData:
        ts = now_ns()
        return MetaData(seq=self._next_seq(), source_ts=ts, local_ts=ts)

    def _jitter_price(self) -> int:
        offset = self._rng.randint(-self._spread, self._spread)
        return self._base_price + offset

    def make_tick(self) -> TickEvent:
        """Create a single synthetic TickEvent."""
        return TickEvent(
            meta=self._make_meta(),
            symbol=self._symbol,
            price=self._jitter_price(),
            volume=self._rng.randint(1, 50),
        )

    def make_bidask(self, levels: int = 5) -> BidAskEvent:
        """Create a synthetic BidAskEvent with *levels* depth."""
        mid = self._jitter_price()
        half = self._spread
        bids = np.array(
            [[mid - half * (i + 1), self._rng.randint(1, 200)] for i in range(levels)],
            dtype=np.int64,
        )
        asks = np.array(
            [[mid + half * (i + 1), self._rng.randint(1, 200)] for i in range(levels)],
            dtype=np.int64,
        )
        return BidAskEvent(
            meta=self._make_meta(),
            symbol=self._symbol,
            bids=bids,
            asks=asks,
        )

    async def generate(
        self,
        queue: asyncio.Queue[Any],
        rate: float,
        duration_s: float,
        symbol: str | None = None,
    ) -> int:
        """Produce ticks at *rate* events/sec for *duration_s* seconds."""
        if symbol is not None:
            self._symbol = symbol
        interval = 1.0 / rate if rate > 0 else 0.0
        count = 0
        deadline = asyncio.get_event_loop().time() + duration_s
        while asyncio.get_event_loop().time() < deadline:
            tick = self.make_tick()
            await queue.put(tick)
            count += 1
            if interval > 0:
                await asyncio.sleep(interval)
        logger.info(
            "generate_done",
            symbol=self._symbol,
            count=count,
            rate=rate,
            duration_s=duration_s,
        )
        return count

    async def generate_burst(
        self,
        queue: asyncio.Queue[Any],
        count: int,
        symbol: str | None = None,
    ) -> int:
        """Produce *count* ticks as fast as possible (burst mode)."""
        if symbol is not None:
            self._symbol = symbol
        for _ in range(count):
            tick = self.make_tick()
            await queue.put(tick)
        logger.info("generate_burst_done", symbol=self._symbol, count=count)
        return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synthetic tick generator for load testing",
    )
    parser.add_argument("--rate", type=float, default=1000.0)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--symbol", type=str, default="2330")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    async def _run() -> None:
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=100_000)
        gen = SyntheticTickGenerator(symbol=args.symbol, seed=args.seed)
        produced = await gen.generate(
            queue,
            rate=args.rate,
            duration_s=args.duration,
            symbol=args.symbol,
        )
        print(f"Produced {produced} events, queue size: {queue.qsize()}")  # noqa: T201

    asyncio.run(_run())


if __name__ == "__main__":
    main()
