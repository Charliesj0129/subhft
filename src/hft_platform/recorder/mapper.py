from typing import Any, Dict, Optional, Tuple

from hft_platform.contracts.execution import FillEvent, OrderEvent
from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
from hft_platform.events import BidAskEvent, TickEvent
from hft_platform.feed_adapter.normalizer import SymbolMetadata


def _descale(symbol: str, value: int | float, metadata: SymbolMetadata, price_codec: PriceCodec | None) -> float:
    if price_codec:
        return price_codec.descale(symbol, value)
    scale = metadata.price_scale(symbol)
    if not scale:
        return float(value)
    return float(value) / float(scale)


def _book_to_arrays(
    levels: Any,
    metadata: SymbolMetadata,
    symbol: str,
    price_codec: PriceCodec | None,
) -> tuple[list[float], list[int]]:
    prices: list[float] = []
    vols: list[int] = []
    if levels is None:
        return prices, vols

    for row in levels:
        if isinstance(row, dict):
            price = row.get("price")
            vol = row.get("volume", 0)
        else:
            price = row[0] if len(row) > 0 else None
            vol = row[1] if len(row) > 1 else 0
        if price is None:
            continue
        prices.append(_descale(symbol, price, metadata, price_codec))
        vols.append(int(vol or 0))

    return prices, vols


def map_event_to_record(
    event: Any,
    metadata: SymbolMetadata,
    price_codec: PriceCodec | None = None,
) -> Optional[Tuple[str, Dict[str, Any]]]:
    if price_codec is None:
        price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(metadata))

    if isinstance(event, TickEvent):
        symbol = event.symbol
        return (
            "market_data",
            {
                "symbol": symbol,
                "exchange": metadata.exchange(symbol),
                "type": "Tick",
                "exch_ts": int(event.meta.source_ts),
                "ingest_ts": int(event.meta.local_ts),
                "price": _descale(symbol, event.price, metadata, price_codec),
                "volume": float(event.volume),
                "bids_price": [],
                "bids_vol": [],
                "asks_price": [],
                "asks_vol": [],
                "seq_no": int(event.meta.seq),
            },
        )

    if isinstance(event, BidAskEvent):
        symbol = event.symbol
        bid_price, bid_vol = _book_to_arrays(event.bids, metadata, symbol, price_codec)
        ask_price, ask_vol = _book_to_arrays(event.asks, metadata, symbol, price_codec)
        return (
            "market_data",
            {
                "symbol": symbol,
                "exchange": metadata.exchange(symbol),
                "type": "Snapshot" if event.is_snapshot else "BidAsk",
                "exch_ts": int(event.meta.source_ts),
                "ingest_ts": int(event.meta.local_ts),
                "price": 0.0,
                "volume": 0.0,
                "bids_price": bid_price,
                "bids_vol": bid_vol,
                "asks_price": ask_price,
                "asks_vol": ask_vol,
                "seq_no": int(event.meta.seq),
            },
        )

    if isinstance(event, OrderEvent):
        symbol = event.symbol
        latency_us = 0.0
        if event.broker_ts_ns and event.ingest_ts_ns:
            latency_us = max(0.0, (event.ingest_ts_ns - event.broker_ts_ns) / 1000.0)
        return (
            "orders",
            {
                "order_id": event.order_id,
                "strategy_id": event.strategy_id,
                "symbol": symbol,
                "side": str(event.side.name if hasattr(event.side, "name") else event.side),
                "price": _descale(symbol, event.price, metadata, price_codec),
                "qty": int(event.submitted_qty),
                "status": str(event.status.name if hasattr(event.status, "name") else event.status),
                "ingest_ts": int(event.ingest_ts_ns),
                "latency_us": float(latency_us),
            },
        )

    if isinstance(event, FillEvent):
        symbol = event.symbol
        return (
            "fills",
            {
                "fill_id": event.fill_id,
                "order_id": event.order_id,
                "strategy_id": event.strategy_id,
                "symbol": symbol,
                "side": str(event.side.name if hasattr(event.side, "name") else event.side),
                "price": _descale(symbol, event.price, metadata, price_codec),
                "qty": int(event.qty),
                "fee": _descale(symbol, event.fee, metadata, price_codec),
                "match_ts": int(event.match_ts_ns),
            },
        )

    return None
