import json
import os
from typing import List

import numpy as np
from structlog import get_logger

try:
    from hftbacktest.types import (
        BUY_EVENT,
        DEPTH_EVENT,
        EXCH_EVENT,
        LOCAL_EVENT,
        SELL_EVENT,
        TRADE_EVENT,
        event_dtype,
    )
except ImportError as exc:  # pragma: no cover - env guard
    event_dtype = None
    DEPTH_EVENT = TRADE_EVENT = BUY_EVENT = SELL_EVENT = EXCH_EVENT = LOCAL_EVENT = 0
    _import_error = exc
else:
    _import_error = None

logger = get_logger("hft_backtest.convert")


def _build_event(ev: int, exch_ts: int, local_ts: int, px: float, qty: float):
    return (ev, int(exch_ts), int(local_ts), float(px), float(qty), 0, 0, 0.0)


def convert_jsonl_to_npz(input_path: str, output_path: str, scale: int = 10_000):
    if event_dtype is None:
        raise RuntimeError(f"hftbacktest not installed: {_import_error}")

    events: List[tuple] = []
    with open(input_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed line", line=line[:100])
                continue

            etype = item.get("type")
            exch_ts = item.get("exch_ts") or item.get("ts") or 0
            local_ts = item.get("local_ts") or exch_ts

            if etype == "BidAsk":
                bids = item.get("bids") or []
                asks = item.get("asks") or []
                if bids:
                    ev = DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT
                    price = bids[0].get("price", 0) / scale
                    vol = bids[0].get("volume", 0)
                    events.append(_build_event(ev, exch_ts, local_ts, price, vol))
                if asks:
                    ev = DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT
                    price = asks[0].get("price", 0) / scale
                    vol = asks[0].get("volume", 0)
                    events.append(_build_event(ev, exch_ts, local_ts, price, vol))
            elif etype == "Tick":
                ev = TRADE_EVENT | EXCH_EVENT | LOCAL_EVENT
                price = item.get("price", 0) / scale
                vol = item.get("volume", 0)
                events.append(_build_event(ev, exch_ts, local_ts, price, vol))
            else:
                logger.debug("Skipping unsupported event type", etype=etype)

    if not events:
        raise ValueError("No events converted; check input data or event types.")

    arr = np.array(events, dtype=event_dtype)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    np.savez_compressed(output_path, data=arr)
    logger.info("Converted feed to hftbacktest npz", output=output_path, count=len(arr))
