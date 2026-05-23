"""P2 #8 (2026-04-27): subscribe-time truncation observability.

Bug context
-----------
RC-1 (2026-04-27) raised the ``ShioajiClient._load_config`` ceiling from
the per-conn cap (``MAX_SUBSCRIPTIONS`` = 120) to the per-client universe
bound (``MAX_SUBSCRIPTIONS_PER_CLIENT`` = 600). That fixed the silent
``ValueError`` in the 25-min reload cycle, but left a NEW silent miss:
``SubscriptionManager.subscribe_basket`` and ``_resubscribe_all`` still
gate the per-symbol loop at ``c.MAX_SUBSCRIPTIONS`` (per-conn 120).

If ``HFT_QUOTE_CONNECTIONS`` is unset (default 1), 121–600 symbols load
into ``self.symbols`` but only the first 120 are ever subscribed — and
prior to this fix the loop simply emitted a single ``logger.error`` line
with no Counter, no alert, no Telegram. Half of the universe could be
missing for the entire trading day before anyone noticed.

Post-fix
--------
1. ``SubscriptionManager`` bumps
   ``feed_subscription_truncate_total{reason="conn_limit"}`` and raises
   the log line to ``severity="critical"`` whenever the per-conn cap is
   reached before all loaded symbols are subscribed (covers BOTH
   ``subscribe_basket`` and ``_resubscribe_all``).
2. ``ShioajiClient._load_config`` advisory preflight bumps
   ``feed_symbol_config_reload_total{result="exceeds_pool_capacity"}``
   when ``len(symbols) > DEFAULT_MAX_SUBSCRIPTIONS_PER_CONN * num_conns``
   (read from ``HFT_QUOTE_CONNECTIONS``) so the issue surfaces at config
   load time instead of waiting for the next subscribe cycle.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
import yaml

from hft_platform.feed_adapter.shioaji.subscription_manager import SubscriptionManager
from hft_platform.feed_adapter.shioaji_client import ShioajiClient
from hft_platform.observability.metrics import MetricsRegistry


def _counter_value(metric, **labels) -> float:
    return metric.labels(**labels)._value.get()


def _make_symbols(count: int) -> List[Dict[str, Any]]:
    return [{"code": f"S{i:04d}", "exchange": "TWSE"} for i in range(count)]


# ---------------------------------------------------------------------------
# Stub harness for subscribe_basket — mirrors the small surface the manager
# touches so we can drive the truncate path without bringing up the real SDK.
# ---------------------------------------------------------------------------


class _NoopMetric:
    """Fallback metric stub for attributes the manager pokes opportunistically."""

    def labels(self, *a: Any, **kw: Any) -> "_NoopMetric":
        return self

    def inc(self, *a: Any, **kw: Any) -> None:
        return None


class _ClientStub:
    """Mimics the slice of ``ShioajiClient`` that ``subscribe_basket`` reads.

    Does NOT inherit from the real client — we want a tight, deterministic
    fake that exercises ONLY the truncation guard. Attribute names match
    the real client to keep ``subscribe_basket`` happy.
    """

    def __init__(
        self,
        symbols: List[Dict[str, Any]],
        max_subscriptions: int,
        notification_dispatcher: Any | None = None,
    ) -> None:
        self.api = object()  # truthy
        self.logged_in = True
        self.tick_callback = None
        self.symbols = symbols
        self.MAX_SUBSCRIPTIONS = max_subscriptions
        self.subscribed_count = 0
        self.subscribed_codes: set[str] = set()
        self._failed_sub_symbols: list[Any] = []
        self._callbacks_registered = True
        self._event_callback_registered = True
        self.mode = "real"
        self._quote_version = None
        self._quote_version_mode = "auto"
        self.fetch_contract = False
        self._last_quote_data_ts = 1.0  # skip first-set branch
        self.metrics = MetricsRegistry.get()
        self._notification_dispatcher = notification_dispatcher

    # subscribe_basket touches the following ↓ — return harmless stubs.
    def _start_quote_dispatch_worker(self) -> None: ...
    def _ensure_callbacks(self, cb: Any) -> None: ...
    def _start_quote_watchdog(self) -> None: ...
    def _start_session_refresh_thread(self) -> None: ...
    def _start_contract_refresh_thread(self) -> None: ...
    def _start_sub_retry_thread(self, cb: Any) -> None: ...
    def _preflight_contracts(self) -> None: ...
    def _refresh_quote_routes(self) -> None: ...

    def _quote_api(self) -> Any:
        # Return a truthy object that has subscribe attribute so the early
        # bailout in subscribe_basket does not trigger.
        class _QuoteAPI:
            def subscribe(self, *a: Any, **kw: Any) -> None: ...

        return _QuoteAPI()


def _patched_subscribe_symbol(self: SubscriptionManager, sym: Dict[str, Any], cb: Any) -> bool:
    """Always succeed — we only test the truncation guard, not real subscribe."""
    return True


# ---------------------------------------------------------------------------
# Tests — Strategy A: subscribe-time truncation guard
# ---------------------------------------------------------------------------


def test_subscribe_basket_bumps_truncate_metric_and_logs_critical(monkeypatch, capsys):
    """P2 #8: 200 symbols + per-conn cap 120 → metric + critical log."""
    metrics = MetricsRegistry.get()
    counter = metrics.feed_subscription_truncate_total
    before = _counter_value(counter, reason="conn_limit")

    client = _ClientStub(symbols=_make_symbols(200), max_subscriptions=120)
    mgr = SubscriptionManager(client)  # type: ignore[arg-type]

    # Skip real broker subscribe — we only test the guard.
    monkeypatch.setattr(SubscriptionManager, "_subscribe_symbol", _patched_subscribe_symbol)

    mgr.subscribe_basket(cb=lambda *a, **kw: None)

    after = _counter_value(counter, reason="conn_limit")
    assert after - before == 1.0, "truncate guard must bump metric exactly once"

    # Per-conn cap honored: subscribed_count never exceeds limit.
    assert client.subscribed_count == 120

    # Per-facade log emitted at the truncation site. structlog formats to
    # stdout/stderr by default — capsys captures both. 2026-05-23 rewrite:
    # template renamed to ``subscription_limit_reached`` with explicit
    # ``shard_size``/``dropped_this_facade`` fields and downgraded to
    # warning; the misleading ``severity="critical"`` string was dropped.
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "subscription_limit_reached" in combined, "truncate event must be logged"
    assert "shard_size=200" in combined, f"truncate log must record shard_size, got: {combined!r}"
    assert "dropped_this_facade=80" in combined, f"truncate log must record drop count, got: {combined!r}"


def test_subscribe_basket_no_metric_bump_when_under_per_conn_cap(monkeypatch):
    """100 symbols + per-conn cap 120 → no truncate metric bump."""
    metrics = MetricsRegistry.get()
    counter = metrics.feed_subscription_truncate_total
    before = _counter_value(counter, reason="conn_limit")

    client = _ClientStub(symbols=_make_symbols(100), max_subscriptions=120)
    mgr = SubscriptionManager(client)  # type: ignore[arg-type]
    monkeypatch.setattr(SubscriptionManager, "_subscribe_symbol", _patched_subscribe_symbol)

    mgr.subscribe_basket(cb=lambda *a, **kw: None)

    after = _counter_value(counter, reason="conn_limit")
    assert after == before, "no truncate when universe ≤ per-conn cap"
    assert client.subscribed_count == 100


def test_subscribe_basket_invokes_dispatcher_on_truncate(monkeypatch):
    """When dispatcher attached, truncate must fire ``notify_subscription_truncated``."""
    captured: dict[str, Any] = {}

    class _DispatcherSpy:
        async def notify_subscription_truncated(
            self,
            *,
            reason: str,
            requested: int,
            subscribed: int,
            limit: int,
            conn_id: str | None = None,
        ) -> None:
            captured["reason"] = reason
            captured["requested"] = requested
            captured["subscribed"] = subscribed
            captured["limit"] = limit
            captured["conn_id"] = conn_id

    client = _ClientStub(
        symbols=_make_symbols(200),
        max_subscriptions=120,
        notification_dispatcher=_DispatcherSpy(),
    )
    # Simulate a pool-attached facade so dispatcher receives conn_id.
    client.conn_id = "0"
    mgr = SubscriptionManager(client)  # type: ignore[arg-type]
    monkeypatch.setattr(SubscriptionManager, "_subscribe_symbol", _patched_subscribe_symbol)

    mgr.subscribe_basket(cb=lambda *a, **kw: None)

    # subscribe_basket runs synchronously (no live loop), so the helper
    # falls back to ``asyncio.run`` and the spy MUST observe the call.
    assert captured == {
        "reason": "conn_limit",
        "requested": 200,
        "subscribed": 120,
        "limit": 120,
        "conn_id": "0",
    }


# ---------------------------------------------------------------------------
# Tests — Strategy B: _load_config preflight pool-capacity advisory
# ---------------------------------------------------------------------------


def test_load_config_exceeds_pool_capacity_single_conn(tmp_path, monkeypatch):
    """588 symbols, HFT_QUOTE_CONNECTIONS unset → exceeds_pool_capacity bump."""
    config_path = tmp_path / "symbols.yaml"
    config_path.write_text(yaml.dump({"symbols": _make_symbols(588)}))

    metrics = MetricsRegistry.get()
    counter = metrics.feed_symbol_config_reload_total
    before_pool = _counter_value(counter, result="exceeds_pool_capacity")
    before_ok = _counter_value(counter, result="ok")

    monkeypatch.delenv("HFT_QUOTE_CONNECTIONS", raising=False)
    monkeypatch.delenv("HFT_ALLOW_TRUNCATE_SUBSCRIPTIONS", raising=False)
    monkeypatch.delenv("HFT_STRICT_SUBSCRIPTION_LIMIT", raising=False)
    monkeypatch.delenv("HFT_MAX_SUBSCRIPTIONS", raising=False)

    client = ShioajiClient(config_path=str(config_path))

    # 588 ≤ 600 (per-client ceiling, no truncate) AND 588 > 120 × 1
    # (per-conn × num_conns) → preflight advisory fires.
    assert len(client.symbols) == 588
    after_pool = _counter_value(counter, result="exceeds_pool_capacity")
    after_ok = _counter_value(counter, result="ok")
    assert after_pool - before_pool == 1.0, "single-conn deploy with 588 symbols MUST bump exceeds_pool_capacity"
    assert after_ok == before_ok, "ok branch must NOT fire when pool capacity is exceeded"


def test_load_config_pool_capacity_ok_with_enough_conns(tmp_path, monkeypatch):
    """588 symbols + HFT_QUOTE_CONNECTIONS=5 → ok (5×120=600 ≥ 588)."""
    config_path = tmp_path / "symbols.yaml"
    config_path.write_text(yaml.dump({"symbols": _make_symbols(588)}))

    metrics = MetricsRegistry.get()
    counter = metrics.feed_symbol_config_reload_total
    before_pool = _counter_value(counter, result="exceeds_pool_capacity")
    before_ok = _counter_value(counter, result="ok")

    monkeypatch.setenv("HFT_QUOTE_CONNECTIONS", "5")
    monkeypatch.delenv("HFT_ALLOW_TRUNCATE_SUBSCRIPTIONS", raising=False)
    monkeypatch.delenv("HFT_STRICT_SUBSCRIPTION_LIMIT", raising=False)
    monkeypatch.delenv("HFT_MAX_SUBSCRIPTIONS", raising=False)

    client = ShioajiClient(config_path=str(config_path))

    assert len(client.symbols) == 588
    after_pool = _counter_value(counter, result="exceeds_pool_capacity")
    after_ok = _counter_value(counter, result="ok")
    assert after_pool == before_pool, "5 × 120 = 600 ≥ 588 → no advisory"
    assert after_ok - before_ok == 1.0, "ok branch must fire when capacity is sufficient"


@pytest.mark.parametrize("bad_value", ["abc", "", "-1"])
def test_load_config_invalid_quote_connections_falls_back_to_one(tmp_path, monkeypatch, bad_value):
    """Garbage HFT_QUOTE_CONNECTIONS → safely treated as 1 (preflight still fires)."""
    config_path = tmp_path / "symbols.yaml"
    config_path.write_text(yaml.dump({"symbols": _make_symbols(300)}))

    metrics = MetricsRegistry.get()
    counter = metrics.feed_symbol_config_reload_total
    before_pool = _counter_value(counter, result="exceeds_pool_capacity")

    monkeypatch.setenv("HFT_QUOTE_CONNECTIONS", bad_value)
    monkeypatch.delenv("HFT_ALLOW_TRUNCATE_SUBSCRIPTIONS", raising=False)
    monkeypatch.delenv("HFT_STRICT_SUBSCRIPTION_LIMIT", raising=False)
    monkeypatch.delenv("HFT_MAX_SUBSCRIPTIONS", raising=False)

    ShioajiClient(config_path=str(config_path))

    after_pool = _counter_value(counter, result="exceeds_pool_capacity")
    assert after_pool - before_pool == 1.0, (
        f"invalid HFT_QUOTE_CONNECTIONS={bad_value!r} must fall back to 1, and 300 > 120 × 1 → exceeds_pool_capacity"
    )
