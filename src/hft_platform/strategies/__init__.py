"""Concrete strategy implementations.

The strategy framework (BaseStrategy, runner, context) lives in
``hft_platform.strategy``.
"""

from .simple_mm import SimpleMarketMaker

__all__ = ["SimpleMarketMaker"]
