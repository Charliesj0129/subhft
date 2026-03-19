"""Tests for WU-14: SyntheticTickGenerator."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from hft_platform.events import BidAskEvent, TickEvent
from hft_platform.testing.load_generator import SyntheticTickGenerator


class TestSyntheticTickGenerator:
    def test_make_tick_returns_tick_event(self) -> None:
        gen = SyntheticTickGenerator(seed=42)
        tick = gen.make_tick()
        assert isinstance(tick, TickEvent)
        assert tick.symbol == "2330"
        assert isinstance(tick.price, int)
        assert tick.price > 0
        assert tick.volume >= 1

    def test_make_tick_price_is_scaled_int(self) -> None:
        gen = SyntheticTickGenerator(base_price=500_0000, spread=1_0000, seed=1)
        tick = gen.make_tick()
        assert 499_0000 <= tick.price <= 501_0000

    def test_make_tick_meta_timestamps(self) -> None:
        gen = SyntheticTickGenerator(seed=7)
        tick = gen.make_tick()
        assert tick.meta.source_ts > 0
        assert tick.meta.local_ts > 0
        assert tick.meta.seq == 1

    def test_make_tick_seq_increments(self) -> None:
        gen = SyntheticTickGenerator(seed=7)
        t1 = gen.make_tick()
        t2 = gen.make_tick()
        assert t1.meta.seq == 1
        assert t2.meta.seq == 2

    def test_make_bidask_returns_bidask_event(self) -> None:
        gen = SyntheticTickGenerator(seed=42)
        ba = gen.make_bidask(levels=5)
        assert isinstance(ba, BidAskEvent)
        assert ba.symbol == "2330"
        assert isinstance(ba.bids, np.ndarray)
        assert isinstance(ba.asks, np.ndarray)
        assert ba.bids.shape == (5, 2)
        assert ba.asks.shape == (5, 2)

    def test_make_bidask_prices_ordered(self) -> None:
        gen = SyntheticTickGenerator(seed=99)
        ba = gen.make_bidask(levels=3)
        bid_prices = ba.bids[:, 0]
        assert all(bid_prices[i] > bid_prices[i + 1] for i in range(len(bid_prices) - 1))
        ask_prices = ba.asks[:, 0]
        assert all(ask_prices[i] < ask_prices[i + 1] for i in range(len(ask_prices) - 1))

    def test_custom_symbol(self) -> None:
        gen = SyntheticTickGenerator(symbol="2317", seed=1)
        tick = gen.make_tick()
        assert tick.symbol == "2317"

    def test_seed_reproducibility(self) -> None:
        g1 = SyntheticTickGenerator(seed=123)
        g2 = SyntheticTickGenerator(seed=123)
        t1 = g1.make_tick()
        t2 = g2.make_tick()
        assert t1.price == t2.price
        assert t1.volume == t2.volume

    @pytest.mark.asyncio
    async def test_generate_produces_events(self) -> None:
        queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        gen = SyntheticTickGenerator(seed=42)
        count = await gen.generate(queue, rate=500, duration_s=0.2)
        assert count > 0
        assert queue.qsize() == count

    @pytest.mark.asyncio
    async def test_generate_respects_symbol_override(self) -> None:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1_000)
        gen = SyntheticTickGenerator(seed=42)
        await gen.generate(queue, rate=100, duration_s=0.1, symbol="2317")
        tick = queue.get_nowait()
        assert tick.symbol == "2317"

    @pytest.mark.asyncio
    async def test_generate_burst(self) -> None:
        queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
        gen = SyntheticTickGenerator(seed=42)
        count = await gen.generate_burst(queue, count=500)
        assert count == 500
        assert queue.qsize() == 500

    @pytest.mark.asyncio
    async def test_generate_burst_symbol_override(self) -> None:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1_000)
        gen = SyntheticTickGenerator(seed=42)
        await gen.generate_burst(queue, count=10, symbol="1301")
        tick = queue.get_nowait()
        assert tick.symbol == "1301"
