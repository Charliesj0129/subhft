"""Shared mock setup for fubon_neo SDK in tests."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock


def install_fubon_neo_mock() -> types.ModuleType:
    """Install a mock fubon_neo.constant module into sys.modules.

    Returns:
        The mock constant module for direct attribute access in tests.
    """
    if "fubon_neo.constant" in sys.modules:
        return sys.modules["fubon_neo.constant"]

    fubon_neo = types.ModuleType("fubon_neo")
    constant = types.ModuleType("fubon_neo.constant")

    constant.BSAction = MagicMock()
    constant.BSAction.Buy = "FUBON_BUY"
    constant.BSAction.Sell = "FUBON_SELL"

    constant.TimeInForce = MagicMock()
    constant.TimeInForce.ROD = "FUBON_ROD"
    constant.TimeInForce.IOC = "FUBON_IOC"
    constant.TimeInForce.FOK = "FUBON_FOK"

    constant.PriceType = MagicMock()
    constant.PriceType.Limit = "FUBON_LIMIT"
    constant.PriceType.Market = "FUBON_MARKET"

    constant.OrderType = MagicMock()
    constant.OrderType.Stock = "FUBON_STOCK"
    constant.OrderType.DayTrade = "FUBON_DAYTRADE"
    constant.OrderType.Margin = "FUBON_MARGIN"

    fubon_neo.constant = constant
    sys.modules["fubon_neo"] = fubon_neo
    sys.modules["fubon_neo.constant"] = constant
    return constant
