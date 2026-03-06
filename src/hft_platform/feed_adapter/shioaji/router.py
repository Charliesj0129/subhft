from __future__ import annotations

import os
import re
import threading
from typing import Any, Callable, Dict, List, cast

from structlog import get_logger

from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("feed_adapter")

# --- Global Callback Registry & Dispatcher ---
# Using global snapshots to avoid per-callback lock contention.
CLIENT_REGISTRY: List[Any] = []
CLIENT_REGISTRY_LOCK = threading.Lock()
CLIENT_REGISTRY_BY_CODE: Dict[str, List[Any]] = {}
CLIENT_REGISTRY_SNAPSHOT: tuple[Any, ...] = ()
CLIENT_REGISTRY_BY_CODE_SNAPSHOT: Dict[str, tuple[Any, ...]] = {}
CLIENT_REGISTRY_WILDCARD_SNAPSHOT: tuple[Any, ...] = ()
CLIENT_DISPATCH_SNAPSHOT: tuple[Callable[..., Any], ...] = ()
CLIENT_DISPATCH_BY_CODE_SNAPSHOT: Dict[str, tuple[Callable[..., Any], ...]] = {}
CLIENT_DISPATCH_WILDCARD_SNAPSHOT: tuple[Callable[..., Any], ...] = ()
TOPIC_CODE_CACHE: Dict[str, str | None] = {}
_TOPIC_CODE_CACHE_MISS = object()
_TOPIC_CODE_CACHE_MAX = max(128, int(os.getenv("HFT_SHIOAJI_TOPIC_CODE_CACHE_MAX", "4096")))
_ROUTE_MISS_STRICT = os.getenv("HFT_SHIOAJI_ROUTE_MISS_STRICT", "0").strip().lower() in {"1", "true", "yes", "on"}
_ROUTE_MISS_FALLBACK_MODE = os.getenv("HFT_SHIOAJI_ROUTE_MISS_FALLBACK", "wildcard").strip().lower()
if _ROUTE_MISS_FALLBACK_MODE not in {"wildcard", "broadcast", "none"}:
    _ROUTE_MISS_FALLBACK_MODE = "wildcard"
_ROUTE_MISS_LOG_EVERY = max(1, int(os.getenv("HFT_SHIOAJI_ROUTE_MISS_LOG_EVERY", "100")))
_ROUTE_MISS_COUNT = 0
_ROUTE_MISS_METRIC = None
_ROUTE_FALLBACK_METRIC = None
_ROUTE_DROP_METRIC = None


def _refresh_registry_snapshots_locked() -> None:
    global CLIENT_REGISTRY_SNAPSHOT, CLIENT_REGISTRY_BY_CODE_SNAPSHOT
    global CLIENT_REGISTRY_WILDCARD_SNAPSHOT
    global CLIENT_DISPATCH_SNAPSHOT, CLIENT_DISPATCH_BY_CODE_SNAPSHOT, CLIENT_DISPATCH_WILDCARD_SNAPSHOT

    def _dispatch_for(client: Any) -> Callable[..., Any] | None:
        cb = getattr(client, "_enqueue_tick", None)
        if cb is not None:
            return cb
        return getattr(client, "_process_tick", None)

    client_snapshot = tuple(CLIENT_REGISTRY)
    CLIENT_REGISTRY_SNAPSHOT = client_snapshot
    CLIENT_REGISTRY_BY_CODE_SNAPSHOT = {
        code: tuple(clients) for code, clients in CLIENT_REGISTRY_BY_CODE.items() if clients
    }
    CLIENT_DISPATCH_SNAPSHOT = tuple(cb for cb in (_dispatch_for(c) for c in client_snapshot) if cb is not None)
    CLIENT_DISPATCH_BY_CODE_SNAPSHOT = {
        code: tuple(cb for cb in (_dispatch_for(c) for c in clients) if cb is not None)
        for code, clients in CLIENT_REGISTRY_BY_CODE_SNAPSHOT.items()
        if clients
    }

    bound_client_ids = {id(c) for clients in CLIENT_REGISTRY_BY_CODE_SNAPSHOT.values() for c in clients}
    wildcard_clients = tuple(
        c for c in client_snapshot if bool(getattr(c, "allow_symbol_fallback", False)) or id(c) not in bound_client_ids
    )
    CLIENT_REGISTRY_WILDCARD_SNAPSHOT = wildcard_clients
    CLIENT_DISPATCH_WILDCARD_SNAPSHOT = tuple(
        cb for cb in (_dispatch_for(c) for c in wildcard_clients) if cb is not None
    )


def _clear_topic_code_cache_locked() -> None:
    TOPIC_CODE_CACHE.clear()


def _record_route_metric(kind: str) -> None:
    global _ROUTE_MISS_METRIC, _ROUTE_FALLBACK_METRIC, _ROUTE_DROP_METRIC
    try:
        metrics = MetricsRegistry.get()
        if kind == "miss":
            if _ROUTE_MISS_METRIC is None:
                _ROUTE_MISS_METRIC = metrics.shioaji_quote_route_total.labels(result="miss")
            _ROUTE_MISS_METRIC.inc()
        elif kind == "fallback":
            if _ROUTE_FALLBACK_METRIC is None:
                _ROUTE_FALLBACK_METRIC = metrics.shioaji_quote_route_total.labels(result="fallback")
            _ROUTE_FALLBACK_METRIC.inc()
        elif kind == "drop":
            if _ROUTE_DROP_METRIC is None:
                _ROUTE_DROP_METRIC = metrics.shioaji_quote_route_total.labels(result="drop")
            _ROUTE_DROP_METRIC.inc()
    except Exception:
        pass


def _extract_quote_code_from_obj(obj: Any) -> str | None:
    if obj is None:
        return None
    if isinstance(obj, dict):
        code = obj.get("code") or obj.get("Code")
        if code:
            return str(code)
        topic = obj.get("topic") or obj.get("Topic")
        if topic:
            return _extract_code_from_topic(str(topic))
        return None
    code = getattr(obj, "code", None) or getattr(obj, "Code", None)
    if code:
        return str(code)
    topic = getattr(obj, "topic", None)
    if topic:
        return _extract_code_from_topic(str(topic))
    if isinstance(obj, str):
        return _extract_code_from_topic(obj)
    return None


def _extract_quote_code(*args: Any, **kwargs: Any) -> str | None:
    # Fast common shapes first: (topic, quote), (exchange, quote), kwargs["quote"].
    if len(args) >= 2:
        code = _extract_quote_code_from_obj(args[1])
        if code:
            return code
        if isinstance(args[0], str):
            code = _extract_code_from_topic(args[0])
            if code:
                return code
    for key in ("quote", "bidask", "tick", "msg", "data"):
        if key in kwargs:
            code = _extract_quote_code_from_obj(kwargs.get(key))
            if code:
                return code
    for item in args:
        code = _extract_quote_code_from_obj(item)
        if code:
            return code
    for item in kwargs.values():
        code = _extract_quote_code_from_obj(item)
        if code:
            return code
    return None


def _extract_code_from_topic(topic: str) -> str | None:
    if not topic:
        return None
    cached = TOPIC_CODE_CACHE.get(topic, _TOPIC_CODE_CACHE_MISS)
    if cached is not _TOPIC_CODE_CACHE_MISS:
        return cast(str | None, cached)

    code: str | None = None
    # Common fast paths avoid regex allocation.
    if topic.startswith("Q/"):
        # e.g. Q/TSE/2330
        last = topic.rsplit("/", 1)[-1]
        if last:
            code = last
    elif topic.startswith("L1:") and ":" in topic:
        # e.g. L1:STK:2330:tick -> third token
        parts = topic.split(":")
        if len(parts) >= 3 and parts[2]:
            code = parts[2]
    elif ":" in topic:
        # e.g. Quote:v1:BidAsk:TXFF202412
        parts = topic.split(":")
        for token in reversed(parts):
            tok = token.strip()
            if not tok:
                continue
            low = tok.lower()
            if low in {"tick", "bidask", "stk", "fop", "quote", "quotes", "l1", "v1"}:
                continue
            if any(ch.isdigit() for ch in tok) or tok.isalpha():
                code = tok
                break
    if code is None:
        # General fallback for topic drift.
        candidates = re.findall(r"[A-Za-z0-9_]+", topic)
        for token in reversed(candidates):
            low = token.lower()
            if low in {"tick", "bidask", "stk", "fop", "quote", "quotes", "l1", "v1"}:
                continue
            if any(ch.isdigit() for ch in token) or token.isalpha():
                code = token
                break
        if code is None:
            for sep in ("/", ":"):
                if sep in topic:
                    parts = [p for p in topic.split(sep) if p]
                    if parts:
                        code = parts[-1]
                        break

    with CLIENT_REGISTRY_LOCK:
        if len(TOPIC_CODE_CACHE) >= _TOPIC_CODE_CACHE_MAX:
            # Simple coarse reset keeps O(1) behavior on hot path.
            TOPIC_CODE_CACHE.clear()
        TOPIC_CODE_CACHE[topic] = code
    return code


def _registry_snapshot(code: str | None = None) -> tuple[tuple[Any, ...], bool]:
    if code:
        routed = CLIENT_REGISTRY_BY_CODE_SNAPSHOT.get(str(code))
        if routed:
            return routed, True
    return CLIENT_REGISTRY_SNAPSHOT, False


def _registry_dispatch_snapshot(code: str | None = None) -> tuple[tuple[Callable[..., Any], ...], bool]:
    if code:
        routed = CLIENT_DISPATCH_BY_CODE_SNAPSHOT.get(str(code))
        if routed:
            return routed, True
    return CLIENT_DISPATCH_SNAPSHOT, False


def _registry_fallback_snapshot() -> tuple[Any, ...]:
    if _ROUTE_MISS_FALLBACK_MODE == "none":
        return ()
    if _ROUTE_MISS_FALLBACK_MODE == "broadcast":
        return CLIENT_REGISTRY_SNAPSHOT
    return CLIENT_REGISTRY_WILDCARD_SNAPSHOT


def _registry_fallback_dispatch_snapshot() -> tuple[Callable[..., Any], ...]:
    if _ROUTE_MISS_FALLBACK_MODE == "none":
        return ()
    if _ROUTE_MISS_FALLBACK_MODE == "broadcast":
        return CLIENT_DISPATCH_SNAPSHOT
    return CLIENT_DISPATCH_WILDCARD_SNAPSHOT


def _registry_register(client: Any) -> None:
    with CLIENT_REGISTRY_LOCK:
        if client not in CLIENT_REGISTRY:
            CLIENT_REGISTRY.append(client)
            _refresh_registry_snapshots_locked()


def _registry_rebind_codes(client: Any, codes: list[str]) -> None:
    with CLIENT_REGISTRY_LOCK:
        for mapped_code, clients in list(CLIENT_REGISTRY_BY_CODE.items()):
            if client in clients:
                clients = [c for c in clients if c is not client]
                if clients:
                    CLIENT_REGISTRY_BY_CODE[mapped_code] = clients
                else:
                    CLIENT_REGISTRY_BY_CODE.pop(mapped_code, None)
        for code in codes:
            key = str(code)
            if not key:
                continue
            bucket = CLIENT_REGISTRY_BY_CODE.setdefault(key, [])
            if client not in bucket:
                bucket.append(client)
        _refresh_registry_snapshots_locked()
        _clear_topic_code_cache_locked()


def _registry_unregister(client: Any) -> None:
    with CLIENT_REGISTRY_LOCK:
        if client in CLIENT_REGISTRY:
            CLIENT_REGISTRY[:] = [c for c in CLIENT_REGISTRY if c is not client]
        for mapped_code, clients in list(CLIENT_REGISTRY_BY_CODE.items()):
            if client in clients:
                clients = [c for c in clients if c is not client]
                if clients:
                    CLIENT_REGISTRY_BY_CODE[mapped_code] = clients
                else:
                    CLIENT_REGISTRY_BY_CODE.pop(mapped_code, None)
        _refresh_registry_snapshots_locked()
        _clear_topic_code_cache_locked()


def dispatch_tick_cb(*args, **kwargs):
    """
    Global static callback to dispatch ticks/bidask to all registered clients.
    Passes through raw args to avoid signature drift across Shioaji callbacks.
    """
    try:
        global _ROUTE_MISS_COUNT
        if not args and not kwargs:
            return
        code = _extract_quote_code(*args, **kwargs)
        dispatchers, routed_exact = _registry_dispatch_snapshot(code)
        if code and not routed_exact:
            _ROUTE_MISS_COUNT += 1
            _record_route_metric("miss")
            if _ROUTE_MISS_STRICT:
                _record_route_metric("drop")
                if _ROUTE_MISS_COUNT % _ROUTE_MISS_LOG_EVERY == 1:
                    logger.warning(
                        "Quote route miss; dropping callback payload",
                        code=code,
                        strict=True,
                        fallback_mode=_ROUTE_MISS_FALLBACK_MODE,
                    )
                return
            dispatchers = _registry_fallback_dispatch_snapshot()
            if not dispatchers:
                _record_route_metric("drop")
                if _ROUTE_MISS_COUNT % _ROUTE_MISS_LOG_EVERY == 1:
                    logger.warning(
                        "Quote route miss; no fallback targets, dropping callback payload",
                        code=code,
                        strict=False,
                        fallback_mode=_ROUTE_MISS_FALLBACK_MODE,
                    )
                return
            _record_route_metric("fallback")
            if _ROUTE_MISS_COUNT % _ROUTE_MISS_LOG_EVERY == 1:
                logger.warning(
                    "Quote route miss; falling back to snapshot",
                    code=code,
                    strict=False,
                    fallback_mode=_ROUTE_MISS_FALLBACK_MODE,
                    fallback_targets=len(dispatchers),
                )
        elif code is None and _ROUTE_MISS_STRICT:
            _ROUTE_MISS_COUNT += 1
            _record_route_metric("miss")
            _record_route_metric("drop")
            if _ROUTE_MISS_COUNT % _ROUTE_MISS_LOG_EVERY == 1:
                logger.warning("Quote route parse miss; dropping callback payload", strict=True)
            return
        elif code is None:
            _ROUTE_MISS_COUNT += 1
            _record_route_metric("miss")
            dispatchers = _registry_fallback_dispatch_snapshot()
            if not dispatchers:
                _record_route_metric("drop")
                if _ROUTE_MISS_COUNT % _ROUTE_MISS_LOG_EVERY == 1:
                    logger.warning(
                        "Quote route parse miss; no fallback targets, dropping callback payload",
                        strict=False,
                        fallback_mode=_ROUTE_MISS_FALLBACK_MODE,
                    )
                return
            _record_route_metric("fallback")
            if _ROUTE_MISS_COUNT % _ROUTE_MISS_LOG_EVERY == 1:
                logger.warning(
                    "Quote route parse miss; falling back to snapshot",
                    strict=False,
                    fallback_mode=_ROUTE_MISS_FALLBACK_MODE,
                    fallback_targets=len(dispatchers),
                )
        for dispatch_fn in dispatchers:
            dispatch_fn(*args, **kwargs)
    except Exception as e:
        logger.error("Global Dispatch Error", error=str(e))


__all__ = [
    "_registry_register",
    "_registry_rebind_codes",
    "_registry_unregister",
    "dispatch_tick_cb",
]
