"""Market data normalization helpers for MarketDataService.

Extracted from market_data.py to reduce module size.
Pure functions — no MarketDataService dependency.
"""

from __future__ import annotations

from typing import Any

from structlog import get_logger

from hft_platform.feed_adapter.shioaji.signatures import detect_crash_signature

logger = get_logger("service.market_data")

_MD_PRICE_FIELDS = (
    "close",
    "price",
    "bid_price",
    "ask_price",
    "bid_volume",
    "ask_volume",
    "buy_price",
    "sell_price",
)
_MD_TIME_FIELDS = ("ts", "datetime")
_MD_CODE_FIELDS = ("code", "symbol")
_MD_NESTED_FIELDS = ("tick", "bidask")


def _looks_like_md(obj: object) -> bool:
    """Return True if *obj* looks like a market-data payload."""
    if obj is None:
        return False
    if isinstance(obj, dict):
        keys = obj.keys()
        if "code" in keys or "symbol" in keys:
            return True
        if (
            "bid_price" in keys
            or "ask_price" in keys
            or "close" in keys
            or "price" in keys
            or "bid_volume" in keys
            or "ask_volume" in keys
            or "buy_price" in keys
            or "sell_price" in keys
        ):
            return True
        return "ts" in keys or "datetime" in keys
    has_code = getattr(obj, "code", None) is not None or getattr(obj, "symbol", None) is not None
    has_price = (
        hasattr(obj, "bid_price")
        or hasattr(obj, "ask_price")
        or hasattr(obj, "close")
        or hasattr(obj, "price")
        or hasattr(obj, "bid_volume")
        or hasattr(obj, "ask_volume")
    )
    has_time = hasattr(obj, "ts") or hasattr(obj, "datetime")
    return bool(has_price or (has_code and (has_price or has_time)))


def _unwrap_md(obj: object) -> object:
    """Unwrap nested tick/bidask payloads from a market-data envelope."""
    if obj is None:
        return obj
    if isinstance(obj, dict):
        tick = obj.get("tick")
        if _looks_like_md(tick):
            return tick
        bidask = obj.get("bidask")
        if _looks_like_md(bidask):
            return bidask
        return obj
    tick = getattr(obj, "tick", None)
    if _looks_like_md(tick):
        return tick
    bidask = getattr(obj, "bidask", None)
    if _looks_like_md(bidask):
        return bidask
    return obj


def _summarize_md(obj: object) -> dict[str, Any]:
    """Return a concise summary dict of a market-data object for logging."""
    if obj is None:
        return {}
    nested: dict[str, str]
    if isinstance(obj, dict):
        keys = list(obj.keys())
        present = [k for k in (*_MD_CODE_FIELDS, *_MD_PRICE_FIELDS, *_MD_TIME_FIELDS) if k in obj]
        nested = {k: type(obj.get(k)).__name__ for k in _MD_NESTED_FIELDS if k in obj}
        return {"keys": keys[:20], "present": present, "nested": nested}
    present = [k for k in (*_MD_CODE_FIELDS, *_MD_PRICE_FIELDS, *_MD_TIME_FIELDS) if hasattr(obj, k)]
    nested = {}
    for k in _MD_NESTED_FIELDS:
        if hasattr(obj, k):
            nested[k] = type(getattr(obj, k)).__name__
    return {"attrs": present, "nested": nested}


def _try_fast_extract_callback_payload(*args: Any, **kwargs: Any) -> tuple[object | None, object | None]:
    """Fast-path extraction of ``(exchange, msg)`` from a Shioaji callback.

    Returns ``(exchange_or_None, md_payload_or_None)``.
    """
    exchange = kwargs.get("exchange")

    for key in ("quote", "tick", "bidask", "data", "msg"):
        if key not in kwargs:
            continue
        candidate = _unwrap_md(kwargs[key])
        if _looks_like_md(candidate):
            return exchange, candidate

    argc = len(args)
    if argc == 2:
        a0, a1 = args
        # Common Shioaji shape: (exchange/topic, msg)
        msg = _unwrap_md(a1)
        if _looks_like_md(msg):
            if exchange is None and (isinstance(a0, str) or hasattr(a0, "name")):
                exchange = a0
            return exchange, msg
        # Alternate order fallback
        msg = _unwrap_md(a0)
        if _looks_like_md(msg):
            if exchange is None and (isinstance(a1, str) or hasattr(a1, "name")):
                exchange = a1
            return exchange, msg
    elif argc == 1:
        msg = _unwrap_md(args[0])
        if _looks_like_md(msg):
            return exchange, msg
    elif argc >= 3:
        # Another common shape: (topic, quote, event) — pick the last MD-like payload quickly.
        for candidate in (args[-1], args[-2], args[0]):
            msg = _unwrap_md(candidate)
            if _looks_like_md(msg):
                if exchange is None and argc > 0 and (isinstance(args[0], str) or hasattr(args[0], "name")):
                    exchange = args[0]
                return exchange, msg

    return exchange, None


def on_shioaji_event(svc: Any, *args: Any, **kwargs: Any) -> None:
    """Unified callback for Shioaji events.

    *svc* is the ``MarketDataService`` instance.
    Signature can vary: ``(exchange, msg)`` or ``(topic, msg, ...)``.
    """
    try:
        if not svc._raw_first_seen:
            svc._raw_first_seen = True
            logger.info(
                "First quote callback",
                args_types=[type(a).__name__ for a in args],
                kwargs_keys=list(kwargs.keys()),
            )
        if svc.log_raw:
            logger.debug("Callback hit", args_len=len(args))

        exchange, msg = _try_fast_extract_callback_payload(*args, **kwargs)
        parse_result = "fast" if msg is not None else "fallback"

        if msg is None:
            # Generic fallback for signature drift across Shioaji versions.
            if exchange is None and "exchange" in kwargs:
                exchange = kwargs["exchange"]
            for arg in args:
                candidate = _unwrap_md(arg)
                looks_like = _looks_like_md(candidate)
                if exchange is None and not looks_like and (hasattr(arg, "name") or isinstance(arg, str)):
                    exchange = arg
                if looks_like:
                    msg = candidate
            if msg is None:
                if len(args) >= 2:
                    msg = _unwrap_md(args[-1])
                elif len(args) == 1:
                    msg = _unwrap_md(args[0])
            if msg is not None:
                msg = _unwrap_md(msg)
            parse_result = "fallback" if msg is not None else "miss"

        if svc.metrics_registry:
            svc._md_callback_parse_counter += 1
            if svc._md_callback_parse_counter % svc._md_callback_parse_metrics_every == 0:
                try:
                    if hasattr(svc.metrics_registry, "market_data_callback_parse_total"):
                        child = svc._md_callback_parse_metric_children.get(parse_result)
                        if child is None:
                            child = svc.metrics_registry.market_data_callback_parse_total.labels(result=parse_result)
                            svc._md_callback_parse_metric_children[parse_result] = child
                        child.inc()
                except Exception:
                    pass

        if not svc.log_raw and msg is not None and not svc._raw_first_parsed:
            svc._raw_first_parsed = True
            logger.info(
                "Quote callback parsed",
                msg_type=type(msg).__name__,
                msg_code=getattr(msg, "code", None),
                exchange=str(exchange) if exchange is not None else None,
                msg_fields=_summarize_md(msg),
            )

        # Enqueue as tuple (exchange, msg) to match consumer
        if hasattr(svc, "loop"):
            if msg is not None:
                svc.loop.call_soon_threadsafe(svc._enqueue_raw, exchange, msg)
            else:
                if svc.log_raw:
                    logger.warning(
                        "Could not parse msg from callback args",
                        args_types=[type(a).__name__ for a in args],
                    )
        else:
            logger.error("Callback loop missing")

    except Exception as e:
        _record_shioaji_crash_signature(svc, str(e), context="md_callback")
        logger.error(f"Error in Shioaji callback: {e}")


def _record_shioaji_crash_signature(svc: Any, text: str | None, *, context: str) -> None:
    """Record a Shioaji crash signature metric if applicable."""
    if not svc.metrics_registry or not hasattr(svc.metrics_registry, "shioaji_crash_signature_total"):
        return
    signature = detect_crash_signature(text)
    if not signature:
        return
    try:
        svc.metrics_registry.shioaji_crash_signature_total.labels(signature=signature, context=context).inc()
    except Exception:
        return
