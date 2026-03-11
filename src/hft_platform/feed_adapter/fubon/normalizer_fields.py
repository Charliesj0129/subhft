"""Fubon-specific normalizer field map for tick/bidask data translation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NormalizerFieldMap:
    """Field-name mapping for broker-specific data formats.

    Each broker uses different JSON keys for the same semantic fields.
    This dataclass captures the mapping so the normalizer can work
    broker-agnostically.
    """

    symbol_key: str = "code"
    price_key: str = "close"
    volume_key: str = "volume"
    ts_key: str = "ts"
    bid_price_key: str = "bid_price"
    ask_price_key: str = "ask_price"
    bid_volume_key: str = "bid_volume"
    ask_volume_key: str = "ask_volume"
    total_volume_key: str = "total_volume"
    simtrade_key: str = "simtrade"
    odd_lot_key: str = "intraday_odd"


# Fubon field names differ from Shioaji defaults
FUBON_FIELD_MAP = NormalizerFieldMap(
    symbol_key="symbol",
    price_key="price",
    volume_key="size",
    ts_key="time",
    bid_price_key="bid_price",  # After flattening from bids[{price,size}]
    ask_price_key="ask_price",  # After flattening from asks[{price,size}]
    bid_volume_key="bid_volume",  # After flattening
    ask_volume_key="ask_volume",  # After flattening
    total_volume_key="volume",
    simtrade_key="isTrial",
    odd_lot_key="intradayOddLot",
)
