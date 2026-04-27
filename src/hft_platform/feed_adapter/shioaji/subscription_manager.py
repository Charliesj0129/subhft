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
from hft_platform.feed_adapter.shioaji.contracts_runtime import derive_callback_code

try:
    import shioaji as _sj
except Exception:  # pragma: no cover - fallback when library absent
    _sj = None

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji.client import ShioajiClient

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

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:  # noqa: C901
        # Complexity: P2 #8 added the truncate-detection branch + post-loop
        # signal which pushed the function from 15 → 16. Extracting the
        # detection block into a helper would obscure the per-iteration
        # control flow that this loop depends on, so the noqa is preferred
        # here. See ``_signal_subscription_truncated`` for the alert path.
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
            # L2: mutate the deque in place — never rebind the attribute,
            # otherwise concurrent ``append`` from peers (or the retry
            # daemon racing this re-init) lands on the orphaned object.
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
            # L2: in-place mutation — see comment above.
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
                # P2 #8 (2026-04-27): RC-1 raised the ``_load_config`` ceiling
                # to MAX_SUBSCRIPTIONS_PER_CLIENT (default 600) but this loop
                # still gates at the per-conn cap (MAX_SUBSCRIPTIONS=120).
                # When a deployment forgets to size ``HFT_QUOTE_CONNECTIONS``,
                # 121–600 symbols are loaded but only the first 120 ever get
                # subscribed. Previously this surfaced only as a single
                # ``logger.error`` line — no Counter, no alert, no Telegram.
                # We now bump ``feed_subscription_truncate_total{reason="conn_limit"}``
                # and raise log severity to ``critical`` so the silent miss is
                # observable. Notification dispatch happens once after the
                # loop to avoid spamming on every iteration.
                truncated_at_limit = True
                logger.error(
                    "Subscription limit reached",
                    limit=c.MAX_SUBSCRIPTIONS,
                    requested=requested,
                    subscribed=c.subscribed_count,
                    severity="critical",
                )
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

        # Capture alias→actual mapping (e.g. TXFR1 → TXFE6)
        # For R1/R2/C0/C1 contracts, contract.code == config code (e.g. "TMFR1"),
        # but callbacks arrive with the resolved month code (e.g. "TMFE6").
        # Derive the actual code from delivery_month/delivery_date.
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

        # D2: try-acquire the resubscribe lock. The lock is owned by the
        # client (added in __init__) and may be absent on legacy mocks; fall
        # back to a per-call lock so tests without mock setup still behave.
        lock = getattr(c, "_resubscribe_lock", None)
        if lock is None:
            # Eager-create on first use; harmless on legacy paths because
            # _resubscribe_all is the only call site that takes it.
            import threading as _t
            c._resubscribe_lock = _t.Lock()  # type: ignore[attr-defined]
            lock = c._resubscribe_lock
        if not lock.acquire(blocking=False):
            metrics = getattr(c, "metrics", None)
            counter = getattr(metrics, "feed_resubscribe_skipped_concurrent_total", None) if metrics else None
            if counter is not None:
                try:
                    counter.inc()
                except Exception:  # noqa: BLE001
                    pass
            logger.debug("resubscribe_skipped_concurrent_caller")
            return

        try:
            # Cooldown RMW now under the lock — no torn read/write.
            now = timebase.now_s()
            last = getattr(c, "_last_resubscribe_ts", 0.0)
            cooldown = getattr(c, "resubscribe_cooldown", 1.5)
            if now - last < cooldown:
                return
            c._last_resubscribe_ts = now  # type: ignore[attr-defined]
            # Unsubscribe existing symbols from broker SDK before re-subscribing
            # to prevent subscription count accumulation on soft recovery.
            old_codes = set(c.subscribed_codes)
            for sym in c.symbols:
                code = sym.get("code")
                if code and code in old_codes:
                    try:
                        self._unsubscribe_symbol(sym)
                    except Exception as exc:
                        logger.debug("unsubscribe_before_resubscribe_failed", code=code, error=str(exc))
            # D2: in-place clear preserves object identity. Peer readers
            # holding a reference (e.g. the watchdog's snapshot path)
            # always see a consistent live object — never an orphaned set.
            c.subscribed_codes.clear()
            c.subscribed_count = 0
            failed: list[dict[str, Any]] = []
            requested = len(c.symbols)
            truncated_at_limit = False
            for sym in c.symbols:
                if c.subscribed_count >= c.MAX_SUBSCRIPTIONS:
                    # P2 #8 (2026-04-27): mirror subscribe_basket. The
                    # resubscribe path also silently dropped 121+ symbols
                    # before this fix.
                    truncated_at_limit = True
                    logger.error(
                        "Subscription limit reached during resubscribe",
                        limit=c.MAX_SUBSCRIPTIONS,
                        requested=requested,
                        subscribed=c.subscribed_count,
                        severity="critical",
                    )
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
            # L2: mutate the deque in place rather than reassigning. The
            # retry daemon may have just appended a peer-thread failure;
            # rebinding to ``failed`` would silently drop it. Resubscribe
            # is a fresh cycle so it is safe to clear pre-existing entries
            # for symbols not in this batch.
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
                if stat in deal_states:
                    on_deal(msg)
                else:
                    on_order(stat, msg)
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
            except Exception as exc:  # noqa: BLE001
                logger.debug("subscription_truncate_metric_bump_failed", error=str(exc))

        dispatcher = getattr(c, "_notification_dispatcher", None)
        if dispatcher is None or not hasattr(dispatcher, "notify_subscription_truncated"):
            return
        try:
            import asyncio as _asyncio  # local import — non hot-path

            coro = dispatcher.notify_subscription_truncated(
                reason=reason,
                requested=requested,
                subscribed=subscribed,
                limit=limit,
            )
            try:
                loop = _asyncio.get_running_loop()
                loop.create_task(coro)
            except RuntimeError:
                # subscribe_basket usually runs on the asyncio loop, but SDK
                # callbacks (event_13/event_4) and worker threads do not.
                # Best-effort run-to-completion; swallow failure so subscribe
                # path never crashes on alert dispatch.
                try:
                    _asyncio.run(coro)
                except Exception as run_exc:  # noqa: BLE001
                    logger.warning(
                        "subscription_truncated_alert_run_failed",
                        error=str(run_exc),
                    )
        except Exception as notify_exc:  # noqa: BLE001
            logger.warning(
                "subscription_truncated_alert_dispatch_failed",
                error=str(notify_exc),
            )
