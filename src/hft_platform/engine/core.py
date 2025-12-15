import asyncio
from typing import List
from hft_platform.strategies.base import Strategy
from hft_platform.feed_adapter.base import FeedAdapter
from hft_platform.order_adapter.base import OrderAdapter
# from hft_platform.rust_core import EventBus # Runtime dependency

class HFTEngine:
    def __init__(self, strategies: List[Strategy], feed: FeedAdapter, exec: OrderAdapter):
        self.strategies = strategies
        self.feed = feed
        self.exec = exec
        # self.bus = EventBus()

    async def run(self):
        """Main event loop."""
        print("Starting HFT Engine...")
        await self.feed.connect()
        # In a real impl, we'd start the bus consumption loop here
        # while True:
        #     event = self.bus.pop()
        #     dispatch(event)
