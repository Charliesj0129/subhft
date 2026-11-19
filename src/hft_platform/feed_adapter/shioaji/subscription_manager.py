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
from hft_platform.core.background_tasks import spawn_retained
from hft_platform.feed_adapter.shioaji._compat import resolve_quote_enum, to_plain_payload
from hft_platform.feed_adapter.shioaji.contracts_runtime import derive_callback_code

try:
    import shioaji as _sj
except Exception:  # pragma: no cover - fallback when library absent
    _sj = None

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji.client import ShioajiClient

logger = get_logger("feed_adapter.subscription_manager")


def _log_truncate_event(client: Any, *, requested: int, phase: str) -> None:
    """Emit the per-facade subscription-truncate event.

    2026-05-23 rewrite: prior template ("Subscription limit reached ...")
    omitted ``conn_id`` and used the misleading field name ``requested`` for
    what is really the per-facade shard size. The HH:04 hourly noise on the
    live engine — driven by the contract-refresh shard-overwrite bug — went
    undiagnosed for weeks because the template hid which conn was affected
    and implied a pool-wide cap miss when only one shard was corrupted.
    Severity downgraded from ``error`` to ``warning`` since the Counter
    ``feed_subscription_truncate_total`` already records the event and no
    alert rule consumed the prior ``severity="critical"`` string tag.
    """
    conn_id = str(getattr(client, "conn_id", "unknown"))
    per_conn_limit = int(getattr(client, "MAX_SUBSCRIPTIONS", 0) or 0)
    subscribed = int(getattr(client, "subscribed_count", 0) or 0)
    dropped = max(0, int(requested) - subscribed)
    symbols = getattr(client, "symbols", None) or []
    sample: list[Any] = []
    for sym in symbols[subscribed : subscribed + 5]:
        if isinstance(sym, dict):
            sample.append(sym.get("code"))
    log = logger.bind(conn_id=conn_id)
    log.warning(
        "subscription_limit_reached",
        phase=phase,
        per_conn_limit=per_conn_limit,
        shard_size=int(requested),
        subscribed_this_facade=subscribed,
        dropped_this_facade=dropped,
        dropped_sample=sample,
        hint=(
            "check shard integrity (shard_size should be ~universe/num_conns); "
            "if shard_size >> expected, suspect refresh_contracts_and_symbols overwrite"
        ),
    )


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

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:  # noqa: C901
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
            c._failed_sub_symbols.clear()
            c._failed_sub_symbols.extend(sym for sym in c.symbols if isinstance(sym, dict))
            if c._failed_sub_symbols:
                c._start_sub_retry_thread(cb)
            c._start_quote_watchdog()
            c._start_session_refresh_thread()
            return

        quote_api = c._quote_api()
        if quote_api is None or not hasattr(quote_api, "subscribe"):
            logger.warning("Quote API unavailable; deferring quote subscription")
            c._failed_sub_symbols.clear()
            c._failed_sub_symbols.extend(sym for sym in c.symbols if isinstance(sym, dict))
            if c._failed_sub_symbols:
                c._start_sub_retry_thread(cb)
            c._start_quote_watchdog()
            c._start_session_refresh_thread()
            return

        if c.fetch_contract:
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
        requested = len(c.symbols)
        truncated_at_limit = False
        for sym in c.symbols:
            if c.subscribed_count >= c.MAX_SUBSCRIPTIONS:
                truncated_at_limit = True
                _log_truncate_event(c, requested=requested, phase="subscribe_basket")
                break
            if self._subscribe_symbol(sym, cb):
                code = sym.get("code")
                if code:
                    c.subscribed_codes.add(code)
                c.subscribed_count = len(c.subscribed_codes)
            else:
                c._failed_sub_symbols.append(sym)
        c._refresh_quote_routes()
        if truncated_at_limit:
            self._signal_subscription_truncated(
                reason="conn_limit",
                requested=requested,
                subscribed=c.subscribed_count,
                limit=int(c.MAX_SUBSCRIPTIONS),
            )
        logger.info("Quote subscription completed", subscribed=c.subscribed_count)
        if c._failed_sub_symbols:
            logger.warning(
                "Failed subscriptions queued for retry",
                count=len(c._failed_sub_symbols),
                codes=[s.get("code") for s in list(c._failed_sub_symbols)[:10]],
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
                except Exception as exc:
                    logger.debug("operation_fallback", error=str(exc))
                    pass
            return False

        actual_code = derive_callback_code(contract, code)
        if actual_code != code:
            c.alias_to_actual[code] = actual_code
            logger.info(
                "symbol_alias_resolved",
                config_code=code,
                actual_code=actual_code,
            )

        quote_api = c._quote_api()
        if quote_api is None or not hasattr(quote_api, "subscribe"):
            logger.error("Quote API unavailable during subscribe", code=code)
            return False

        try:
            start_ns = time.perf_counter_ns()
            v = c._get_quote_version()
            quote_type = resolve_quote_enum(sj, "QuoteType")
            if v is None:
                quote_api.subscribe(contract, quote_type=quote_type.Tick)
                quote_api.subscribe(contract, quote_type=quote_type.BidAsk)
            else:
                quote_api.subscribe(contract, quote_type=quote_type.Tick, version=v)
                quote_api.subscribe(contract, quote_type=quote_type.BidAsk, version=v)
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
            quote_type = resolve_quote_enum(sj, "QuoteType")
            if v is None:
                quote_api.unsubscribe(contract, quote_type=quote_type.Tick)
                quote_api.unsubscribe(contract, quote_type=quote_type.BidAsk)
            else:
                quote_api.unsubscribe(contract, quote_type=quote_type.Tick, version=v)
                quote_api.unsubscribe(contract, quote_type=quote_type.BidAsk, version=v)
            c._record_api_latency("unsubscribe", start_ns, ok=True)
        except Exception as e:
            c._record_api_latency("unsubscribe", start_ns, ok=False)
            logger.warning(f"Unsubscribe failed for {code}: {e}")

    def _resubscribe_all(self) -> None:  # noqa: C901  # D2: lock+cooldown+loop adds branches; refactor would obscure the lock contract
        """Re-subscribe all symbols, typically after a reconnect or recovery.

        D2: ``_resubscribe_lock`` serializes calls from the 4 caller threads
        (watchdog, schedule_resubscribe daemon, SDK event_13/event_4 thread,
        ``MarketDataService._attempt_resubscribe`` via ``to_thread``).
        Concurrent callers no-op (``acquire(blocking=False)``) and bump
        ``feed_resubscribe_skipped_concurrent_total``.
        """
        c = self._client
        if not c.api or not c.logged_in or not c.tick_callback:
            return
        c._ensure_callbacks(c.tick_callback)
        if not (c._callbacks_registered and c._event_callback_registered):
            return
        quote_api = c._quote_api()
        if quote_api is None or not hasattr(quote_api, "subscribe"):
            return

        lock = getattr(c, "_resubscribe_lock", None)
        if lock is None:
            import threading as _t

            c._resubscribe_lock = _t.Lock()  # type: ignore[attr-defined]
            lock = c._resubscribe_lock
        if not lock.acquire(blocking=False):
            metrics = getattr(c, "metrics", None)
            counter = getattr(metrics, "feed_resubscribe_skipped_concurrent_total", None) if metrics else None
            if counter is not None:
                try:
                    counter.inc()
                except Exception:
                    pass
            logger.debug("resubscribe_skipped_concurrent_caller")
            return

        try:
            now = timebase.now_s()
            last = getattr(c, "_last_resubscribe_ts", 0.0)
            cooldown = getattr(c, "resubscribe_cooldown", 1.5)
            if now - last < cooldown:
                return
            c._last_resubscribe_ts = now  # type: ignore[attr-defined]
            old_codes = set(c.subscribed_codes)
            for sym in c.symbols:
                code = sym.get("code")
                if code and code in old_codes:
                    try:
                        self._unsubscribe_symbol(sym)
                    except Exception as exc:
                        logger.debug("unsubscribe_before_resubscribe_failed", code=code, error=str(exc))
            settle_s = float(os.getenv("HFT_RESUBSCRIBE_UNSUBSCRIBE_SETTLE_S", "0.2"))
            if settle_s > 0:
                time.sleep(settle_s)
            c.subscribed_codes.clear()
            c.subscribed_count = 0
            failed: list[dict[str, Any]] = []
            requested = len(c.symbols)
            truncated_at_limit = False
            for sym in c.symbols:
                if c.subscribed_count >= c.MAX_SUBSCRIPTIONS:
                    truncated_at_limit = True
                    _log_truncate_event(c, requested=requested, phase="resubscribe")
                    break
                if self._subscribe_symbol(sym, c.tick_callback):
                    code = sym.get("code")
                    if code:
                        c.subscribed_codes.add(code)
                    c.subscribed_count = len(c.subscribed_codes)
                else:
                    failed.append(sym)
            c._refresh_quote_routes()
        finally:
            lock.release()

        if truncated_at_limit:
            self._signal_subscription_truncated(
                reason="conn_limit",
                requested=requested,
                subscribed=c.subscribed_count,
                limit=int(c.MAX_SUBSCRIPTIONS),
            )

        if failed:
            c._failed_sub_symbols.clear()
            c._failed_sub_symbols.extend(failed)
            logger.warning(
                "Resubscribe had failures, queuing retry",
                count=len(failed),
                codes=[s.get("code") for s in failed[:10]],
            )
            c._start_sub_retry_thread(c.tick_callback)

    def resubscribe(self) -> bool:
        """User-facing resubscribe with metrics tracking."""
        c = self._client
        if not c.api or not c.logged_in or not c.tick_callback:
            if c.metrics:
                c.metrics.feed_resubscribe_total.labels(result="skip").inc()
            return False
        try:
            self._resubscribe_all()
            if c.metrics:
                c.metrics.feed_resubscribe_total.labels(result="ok").inc()
            return True
        except Exception as exc:
            logger.error("Resubscribe failed", error=str(exc))
            if c.metrics:
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
                payload = to_plain_payload(msg)
                if stat in deal_states:
                    on_deal(payload)
                else:
                    on_order(stat, payload)
            except Exception as exc:
                logger.error("Execution callback failed", error=str(exc))

        c._order_callback = _order_cb  # type: ignore[attr-defined]
        c.api.set_order_callback(c._order_callback)  # type: ignore[attr-defined]

    def _signal_subscription_truncated(
        self,
        *,
        reason: str,
        requested: int,
        subscribed: int,
        limit: int,
    ) -> None:
        """Bump the truncate metric and best-effort dispatch a critical alert.

        P2 #8 fix (2026-04-27): RC-1 closed the ``_load_config`` silent-miss
        but left the per-conn truncation in ``subscribe_basket`` /
        ``_resubscribe_all`` silent. This helper bumps
        ``feed_subscription_truncate_total{reason}`` and fans the event out
        to the optional notification dispatcher attached to the client. Both
        operations are guarded — neither metric bookkeeping nor alert
        dispatch is allowed to crash the subscription pipeline.
        """
        c = self._client
        metrics = getattr(c, "metrics", None)
        counter = getattr(metrics, "feed_subscription_truncate_total", None) if metrics else None
        if counter is not None:
            try:
                counter.labels(reason=reason).inc()
            except Exception as exc:
                logger.debug("subscription_truncate_metric_bump_failed", error=str(exc))

        dispatcher = getattr(c, "_notification_dispatcher", None)
        if dispatcher is None or not hasattr(dispatcher, "notify_subscription_truncated"):
            return
        try:
            import asyncio as _asyncio

            coro = dispatcher.notify_subscription_truncated(
                reason=reason,
                requested=requested,
                subscribed=subscribed,
                limit=limit,
                conn_id=str(getattr(c, "conn_id", "unknown")),
            )
            try:
                spawn_retained(coro, name="notify_subscription_truncated")
            except RuntimeError:
                try:
                    _asyncio.run(coro)
                except Exception as run_exc:
                    logger.warning(
                        "subscription_truncated_alert_run_failed",
                        error=str(run_exc),
                    )
        except Exception as notify_exc:
            logger.warning(
                "subscription_truncated_alert_dispatch_failed",
                error=str(notify_exc),
            )
