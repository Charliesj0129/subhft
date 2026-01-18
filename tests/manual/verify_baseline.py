import asyncio
import time
from dataclasses import dataclass
from typing import List

print("Starting Import...", flush=True)


# Basic Mocks for missing infrastructure
@dataclass
class MockStrategy:
    strategy_id: str = "test_strat"
    enabled: bool = True
    symbols: List[str] = None

    def on_book(self, ctx, event):
        # Place order logic
        print(f"[Strategy] on_book: Can place order here. Price: {event.get('close')}")
        # Test: Place Limit Order at current price
        intent = ctx.place_order(
            symbol=event["symbol"],
            side=1,  # Buy
            price=event.get("close") / 10000.0,  # Pass float, expect scaling
            qty=1,
        )
        return [intent]

    def on_fill(self, ctx, event):
        print(f"[Strategy] on_fill: Filled {event.qty} @ {event.price}")
        return []

    def on_order(self, ctx, event):
        print(f"[Strategy] on_order: Status {event.status}")
        return []


async def test_baseline():
    print("=== System Baseline Verification ===")

    # 1. Setup Components
    print("Importing StrategyRunner...", flush=True)
    from hft_platform.strategy.runner import StrategyRunner

    print("Importing BaseStrategy...", flush=True)
    print("Importing FillEvent...", flush=True)
    from hft_platform.contracts.execution import FillEvent

    print("Imports Done.", flush=True)

    # Mock Bus
    class MockBus:
        def __init__(self):
            self.queue = asyncio.Queue()

        async def publish(self, event):
            await self.queue.put(event)

        async def consume(self):
            while True:
                yield await self.queue.get()

    bus = MockBus()
    risk_queue = asyncio.Queue()

    class MockLOB:
        def get_book_snapshot(self, symbol):
            return {"bids": [], "asks": []}  # Empty snapshot

    print("Initializing StrategyRunner...", flush=True)
    runner = StrategyRunner(bus, risk_queue, lob_engine=MockLOB(), config_path="tests/manual/empty_strat.yaml")
    print("Runner Initialized.", flush=True)

    # Patch Strategy
    strat = MockStrategy(symbols=["2330"])
    # Determine how to register mock strategy since runner uses registry
    # We can perform direct injection
    runner.strategies = [strat]

    print("[1] Components Initialized.")

    # 2. Inject Market Data (Dict)
    # Scale: 100.0 -> 1,000,000
    tick = {
        "type": "Tick",
        "topic": "market_data",
        "symbol": "2330",
        "close": 1_000_000,  # Scaled int
        "volume": 5,
        "ts": time.time_ns(),
    }

    print(f"[2] Injecting Tick: {tick}")

    # Run process_event manually to test dispatch
    await runner.process_event(tick)

    # 3. Verify OrderIntent
    if not risk_queue.empty():
        intent = await risk_queue.get()
        print(f"[3] Risk Queue received Intent: {intent}")
        assert intent.price == 1_000_000, f"Price Scaling Failed: got {intent.price}"
        assert intent.symbol == "2330"
        print("    -> Price Scaling: PASS (x10000 verified)")
    else:
        print("    -> FAIL: No intent generated")
        return

    # 4. Inject Fill Event
    # Construct FillEvent directly
    fill = FillEvent(
        fill_id="f1",
        account_id="sim",
        order_id="o1",
        strategy_id="test_strat",
        symbol="2330",
        side=1,
        qty=1,
        price=1_000_000,
        fee=0,
        tax=0,
        match_ts_ns=time.time_ns(),
        ingest_ts_ns=time.time_ns(),
    )

    print(f"[4] Injecting Fill: {fill}")
    await runner.process_event(fill)
    # Output should show [Strategy] on_fill

    print("=== Verification Complete ===")


if __name__ == "__main__":
    asyncio.run(test_baseline())
