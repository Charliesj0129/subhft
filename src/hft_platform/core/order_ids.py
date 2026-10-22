from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Dict, Mapping, Optional


class _NullLock:
    """No-op lock used when the resolver has not been wired with a threading.Lock.

    Keeps the `with` / `__enter__` protocol working so iteration paths stay
    single-codepath whether or not a real lock is injected.
    """

    __slots__ = ()

    def __enter__(self) -> "_NullLock":
        return self

    def __exit__(self, *exc: Any) -> None:  # pragma: no cover — trivial
        return None


_NULL_LOCK: _NullLock = _NullLock()


@dataclass(slots=True)
class OrderIdResolver:
    order_id_map: Dict[str, Any]
    # P0-E1: optional threading.Lock injected by OrderAdapter bootstrap so
    # broker-thread reads can briefly lock against main-loop writes. Defaults
    # to a no-op lock for tests and callers that do not cross threads.
    lock: ContextManager[Any] = field(default_factory=lambda: _NULL_LOCK)
    # "Is this order key still in flight?" Wired by bootstrap to the OrderAdapter.
    # The broker recycles ``seqno`` / ``id`` (the order ack of THESHOW 2026-10-06
    # 09:35 matched one registered by an order that ended 11 h earlier and was
    # attributed to it), so a recycled id may only claim an order that is still
    # live. ``ordno`` never repeats and is exempt. ``None`` = no opinion: every
    # mapping counts, which is what a standalone resolver (tests) wants.
    is_live: Callable[[str], bool] | None = None

    def key_is_live(self, order_key: Optional[str]) -> bool:
        check = self.is_live
        if check is None or not order_key:
            return True
        try:
            return bool(check(order_key))
        except Exception:  # noqa: BLE001 — a broken check must not hide an order: fall back to trusting the map
            return True

    def normalize_order_key(self, raw: Any) -> Optional[str]:
        if raw is None:
            return None
        if isinstance(raw, dict):
            strat = raw.get("strategy_id")
            intent_id = raw.get("intent_id")
            if strat and intent_id is not None:
                return f"{strat}:{intent_id}"
            if strat:
                return str(strat)
            return None
        if isinstance(raw, (list, tuple)):
            if len(raw) >= 2:
                return f"{raw[0]}:{raw[1]}"
            if raw:
                return str(raw[0])
            return None
        return str(raw)

    def resolve_order_key(
        self,
        strategy_id: str,
        order_id: Any,
        live_orders: Mapping[str, Any] | None = None,
    ) -> str:
        if order_id is None:
            return f"{strategy_id}:"

        order_key = str(order_id) if ":" in str(order_id) else f"{strategy_id}:{order_id}"

        if live_orders is not None and order_key in live_orders:
            return order_key

        mapped = self.order_id_map.get(str(order_id))
        if mapped:
            resolved = self.normalize_order_key(mapped)
            if resolved:
                return resolved

        return order_key

    def resolve_strategy_id(self, order_id: str, *, live_only: bool = False) -> str:
        order_key = self.resolve_order_key_candidate(order_id, live_only=live_only)
        if not order_key:
            return "UNKNOWN"
        if ":" in order_key:
            return order_key.split(":", 1)[0]
        return order_key

    def resolve_order_key_candidate(self, order_id: Any, *, live_only: bool = False) -> Optional[str]:
        """Order key registered for ``order_id``; with ``live_only`` only if that order is still live."""
        if order_id is None:
            return None
        order_id_str = str(order_id)
        # P0-E1: hold the lock across BOTH the direct get and the prefix-match
        # snapshot so a writer that appears between the two reads cannot leave
        # the resolver wedged on a stale dict view. Tuple materialisation is
        # what actually prevents `RuntimeError: dictionary changed size during
        # iteration`; the lock additionally prevents the CPython-internal
        # half-resized dict state that is officially undefined behaviour.
        with self.lock:
            order_key = self.normalize_order_key(self.order_id_map.get(order_id_str))
            if order_key and (not live_only or self.key_is_live(order_key)):
                return order_key
            if not order_id_str:
                return None
            # Prefix-match fallback: Shioaji ordno grows from "vA0G6" (order)
            # to "vA0G671S" (fill) — the fill ordno starts with the order ordno.
            snapshot = tuple(self.order_id_map.items())
        for registered_id, mapped_key in snapshot:
            if registered_id and order_id_str.startswith(str(registered_id)):
                order_key = self.normalize_order_key(mapped_key)
                if order_key and (not live_only or self.key_is_live(order_key)):
                    return order_key
        return None

    def resolve_order_key_from_candidates(self, candidates: list[str], strong: int | None = None) -> Optional[str]:
        """First candidate that maps to an order key.

        ``strong`` = how many leading candidates are ids that never repeat
        (``ordno``); the rest only count while their order is live. ``None``
        keeps every candidate unconditional.
        """
        for index, candidate in enumerate(candidates):
            if not candidate:
                continue
            order_key = self.resolve_order_key_candidate(candidate, live_only=strong is not None and index >= strong)
            if order_key:
                return order_key
        return None

    def resolve_strategy_id_from_candidates(self, candidates: list[str], strong: int | None = None) -> str:
        for index, candidate in enumerate(candidates):
            if not candidate:
                continue
            resolved = self.resolve_strategy_id(candidate, live_only=strong is not None and index >= strong)
            if resolved and resolved != "UNKNOWN":
                return resolved
        return "UNKNOWN"
