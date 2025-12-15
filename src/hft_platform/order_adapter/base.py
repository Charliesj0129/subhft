from abc import ABC, abstractmethod
from typing import Any

class OrderAdapter(ABC):
    @abstractmethod
    async def place_order(self, order: Any) -> Any:
        """Place a new order."""
        pass

    @abstractmethod
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an existing order."""
        pass

    @abstractmethod
    async def get_position(self) -> Any:
        """Get current positions."""
        pass
