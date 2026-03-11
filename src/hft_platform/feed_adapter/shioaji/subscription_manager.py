"""Subscription management for Shioaji quote feeds.

Phase-6 decoupling: owns subscribe_basket, _subscribe_symbol, _unsubscribe_symbol,
_resubscribe_all, resubscribe, and set_execution_callbacks.
ShioajiClient stubs delegate here.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any, Callable, Dict

from structlog import get_logger

from hft_platform.core import timebase

try:
    import shioaji as _sj
except Exception:  # pragma: no cover - fallback when library absent
    _sj = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

logger = get_logger("feed_adapter.subscription_manager")


class SubscriptionManager:
    """Manages symbol subscription lifecycle for Shioaji quote feeds.

    Responsibilities:
    - subscribe_basket: orchestrate initial subscription of all configured symbols
    - _subscribe_symbol / _unsubscribe_symbol: per-symbol quote subscription
    - _resubscribe_all / resubscribe: bulk re-subscription after reconnect
    - set_execution_callbacks: order/deal callback registration
    """

    __slots__ = ("_client",)

    def __init__(self, client: ShioajiClient) -> None:
        self._client = client

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:
        """Subscribe to all configured symbols.

        Orchestrates callback registration, contract preflight, per-symbol
        subscription, and starts background threads (watchdog, session refresh,
        subscription retry).
        """
        c = self._client
        if not c.api:
            logger.info("Shioaji lib missing: skipping real subscription")
            return

        if not c.logged_in:
            logger.warning("Not logged in; skipping subscription.")
            return

        c.tick_callback = cb
        c._start_quote_dispatch_worker()
        c._ensure_callbacks(cb)
        if not (c._callbacks_registered and c._event_callback_registered):
            logger.warning(
                "Quote callbacks not ready; deferring quote subscription",
                callbacks_registered=c._callbacks_registered,
                event_callback_registered=c._event_callback_registered,
            )
            c._failed_sub_symbols = [sym for sym in c.symbols if isinstance(sym, dict)]
            if c._failed_sub_symbols:
                c._start_sub_retry_thread(cb)
            c._start_quote_watchdog()
            c._start_session_refresh_thread()
            return

        quote_api = c._quote_api()
        if quote_api is None or not hasattr(quote_api, "subscribe"):
            logger.warning("Quote API unavailable; deferring quote subscription")
            c._failed_sub_symbols = [sym for sym in c.symbols if isinstance(sym, dict)]
            if c._failed_sub_symbols:
                c._start_sub_retry_thread(cb)
            c._start_quote_watchdog()
            c._start_session_refresh_thread()
            return

        c._start_contract_refresh_thread()
        if c._last_quote_data_ts <= 0:
            c._last_quote_data_ts = timebase.now_s()

        if os.getenv("HFT_CONTRACT_PREFLIGHT", "1") == "1":
            c._preflight_contracts()

        logger.info(
            "Subscribing quote basket",
            count=len(c.symbols),
            mode=c.mode,
            quote_version=c._quote_version,
            quote_version_mode=c._quote_version_mode,
        )
        for sym in c.symbols:
            if c.subscribed_count >= c.MAX_SUBSCRIPTIONS:
                logger.error("Subscription limit reached", limit=c.MAX_SUBSCRIPTIONS)
                break
            if self._subscribe_symbol(sym, cb):
                code = sym.get("code")
                if code:
                    c.subscribed_codes.add(code)
                c.subscribed_count = len(c.subscribed_codes)
            else:
                c._failed_sub_symbols.append(sym)
        c._refresh_quote_routes()
        logger.info("Quote subscription completed", subscribed=c.subscribed_count)
        if c._failed_sub_symbols:
            logger.warning(
                "Failed subscriptions queued for retry",
                count=len(c._failed_sub_symbols),
                codes=[s.get("code") for s in c._failed_sub_symbols[:10]],
            )
            c._start_sub_retry_thread(cb)
        c._start_quote_watchdog()
        c._start_session_refresh_thread()

    def _subscribe_symbol(self, sym: Dict[str, Any], cb: Callable[..., Any]) -> bool:
        """Subscribe to a single symbol's Tick and BidAsk quote feeds."""
        c = self._client
        sj = _sj

        code = sym.get("code")
        exchange = sym.get("exchange")
        product_type = sym.get("product_type") or sym.get("security_type") or sym.get("type")
        if not code or not exchange:
            logger.error("Invalid symbol entry", symbol=sym)
            return False

        contract = c._get_contract(
            exchange,
            code,
            product_type=product_type,
            allow_synthetic=c.allow_synthetic_contracts,
        )
        if not contract:
            logger.error("Contract not found", code=code)
            if hasattr(c.metrics, "shioaji_contract_lookup_errors_total"):
                try:
                    c.metrics.shioaji_contract_lookup_errors_total.labels(code=str(code)).inc()
                except Exception:
                    pass
            return False

        quote_api = c._quote_api()
        if quote_api is None or not hasattr(quote_api, "subscribe"):
            logger.error("Quote API unavailable during subscribe", code=code)
            return False

        try:
            start_ns = time.perf_counter_ns()
            v = c._get_quote_version()
            if v is None:
                quote_api.subscribe(contract, quote_type=sj.constant.QuoteType.Tick)
                quote_api.subscribe(contract, quote_type=sj.constant.QuoteType.BidAsk)
            else:
                quote_api.subscribe(contract, quote_type=sj.constant.QuoteType.Tick, version=v)
                quote_api.subscribe(contract, quote_type=sj.constant.QuoteType.BidAsk, version=v)
            c._record_api_latency("subscribe", start_ns, ok=True)
            return True
        except Exception as e:
            c._record_api_latency("subscribe", start_ns, ok=False)
            c._record_crash_signature(str(e), context="subscribe_symbol")
            logger.error(f"Subscription failed for {code}: {e}")
            return False

    def _unsubscribe_symbol(self, sym: Dict[str, Any]) -> None:
        """Unsubscribe from a single symbol's quote feeds."""
        c = self._client
        sj = _sj

        if not c.api or not sj:
            return
        quote_api = c._quote_api()
        if quote_api is None or not hasattr(quote_api, "unsubscribe"):
            return
        code = sym.get("code")
        exchange = sym.get("exchange")
        product_type = sym.get("product_type") or sym.get("security_type") or sym.get("type")
        if not code or not exchange:
            return
        contract = c._get_contract(exchange, code, product_type=product_type, allow_synthetic=False)
        if not contract:
            return
        try:
            start_ns = time.perf_counter_ns()
            v = c._get_quote_version()
            if v is None:
                quote_api.unsubscribe(contract, quote_type=sj.constant.QuoteType.Tick)
                quote_api.unsubscribe(contract, quote_type=sj.constant.QuoteType.BidAsk)
            else:
                quote_api.unsubscribe(contract, quote_type=sj.constant.QuoteType.Tick, version=v)
                quote_api.unsubscribe(contract, quote_type=sj.constant.QuoteType.BidAsk, version=v)
            c._record_api_latency("unsubscribe", start_ns, ok=True)
        except Exception as e:
            c._record_api_latency("unsubscribe", start_ns, ok=False)
            logger.warning(f"Unsubscribe failed for {code}: {e}")

    def _resubscribe_all(self) -> None:
        """Re-subscribe all symbols, typically after a reconnect or recovery."""
        c = self._client
        if not c.api or not c.logged_in or not c.tick_callback:
            return
        c._ensure_callbacks(c.tick_callback)
        if not (c._callbacks_registered and c._event_callback_registered):
            return
        quote_api = c._quote_api()
        if quote_api is None or not hasattr(quote_api, "subscribe"):
            return
        now = timebase.now_s()
        last = getattr(c, "_last_resubscribe_ts", 0.0)
        cooldown = getattr(c, "resubscribe_cooldown", 1.5)
        if now - last < cooldown:
            return
        c._last_resubscribe_ts = now
        c.subscribed_codes = set()
        c.subscribed_count = 0
        for sym in c.symbols:
            if c.subscribed_count >= c.MAX_SUBSCRIPTIONS:
                logger.error("Subscription limit reached during resubscribe", limit=c.MAX_SUBSCRIPTIONS)
                break
            if self._subscribe_symbol(sym, c.tick_callback):
                code = sym.get("code")
                if code:
                    c.subscribed_codes.add(code)
                c.subscribed_count = len(c.subscribed_codes)
        c._refresh_quote_routes()

    def resubscribe(self) -> bool:
        """User-facing resubscribe with metrics tracking."""
        c = self._client
        if not c.api or not c.logged_in or not c.tick_callback:
            c.metrics.feed_resubscribe_total.labels(result="skip").inc()
            return False
        try:
            self._resubscribe_all()
            c.metrics.feed_resubscribe_total.labels(result="ok").inc()
            return True
        except Exception as exc:
            logger.error("Resubscribe failed", error=str(exc))
            c.metrics.feed_resubscribe_total.labels(result="error").inc()
            return False

    def set_execution_callbacks(self, on_order: Callable[..., Any], on_deal: Callable[..., Any]) -> None:
        """Register low-latency order/deal callbacks on the Shioaji API.

        Note: These run on Shioaji threads.
        """
        c = self._client
        sj = _sj

        if not c.api:
            logger.warning("Shioaji SDK missing; execution callbacks not registered (sim mode).")
            return
        order_state = getattr(sj.constant, "OrderState", None) if sj else None
        deal_states: set[Any] = set()
        if order_state:
            for name in ("StockDeal", "FuturesDeal"):
                state = getattr(order_state, name, None)
                if state is not None:
                    deal_states.add(state)

        def _order_cb(stat: Any, msg: Any) -> None:
            try:
                if stat in deal_states:
                    on_deal(msg)
                else:
                    on_order(stat, msg)
            except Exception as exc:
                logger.error("Execution callback failed", error=str(exc))

        c._order_callback = _order_cb
        c.api.set_order_callback(c._order_callback)
