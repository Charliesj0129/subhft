from abc import ABC, abstractmethod
from typing import Callable, Any

class FeedAdapter(ABC):
    def __init__(self):
        self._callback: Callable[[Any], None] | None = None

    def set_callback(self, callback: Callable[[Any], None]):
        """Set the callback to push events to the Event Bus."""
        self._callback = callback

    @abstractmethod
    async def connect(self):
        """Connect to the market data source."""
        pass

    @abstractmethod
    async def subscribe(self, symbols: list[str]):
        """Subscribe to market data for symbols."""
        pass

    @abstractmethod
    async def disconnect(self):
        """Disconnect from the source."""
        pass
