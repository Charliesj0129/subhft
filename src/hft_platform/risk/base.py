from abc import ABC, abstractmethod
from typing import Any


class RiskManager(ABC):
    @abstractmethod
    def check_order(self, order: Any) -> bool:
        """Check if order passes risk limits."""
        pass

    @abstractmethod
    def on_fill(self, fill: Any):
        """Update risk state on fill."""
        pass


class StormGuard:
    def __init__(self):
        self.triggered = False

    def check(self) -> bool:
        """Check if circuit breaker should trigger."""
        return not self.triggered
