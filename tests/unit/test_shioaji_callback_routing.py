from __future__ import annotations
from unittest.mock import MagicMock

from hft_platform.feed_adapter import shioaji_client as mod


class _DummyClient:
    def __init__(self, name: str):
        self.name = name
        self.allow_symbol_fallback = False
        self._enqueue_tick = MagicMock()


def test_dispatch_tick_cb_routes_by_code():
    c1 = _DummyClient("c1")
    c2 = _DummyClient("c2")

    with mod.CLIENT_REGISTRY_LOCK:
        mod.CLIENT_REGISTRY.clear()
        mod.CLIENT_REGISTRY_BY_CODE.clear()

    mod._registry_register(c1)
    mod._registry_register(c2)
    mod._registry_rebind_codes(c1, ["2330"])
    mod._registry_rebind_codes(c2, ["2317"])

    class Quote:
        code = "2330"

    mod.dispatch_tick_cb("Q/TSE/2330", Quote())

    c1._enqueue_tick.assert_called_once()
    c2._enqueue_tick.assert_not_called()


def test_extract_code_from_topic_variants():
    assert mod._extract_code_from_topic("Q/TSE/2330") == "2330"
    assert mod._extract_code_from_topic("Quote:v1:BidAsk:TXFF202412") == "TXFF202412"
    assert mod._extract_code_from_topic("L1:STK:2330:tick") == "2330"


def test_enqueue_tick_drops_when_queue_full(monkeypatch):
    dummy = object.__new__(mod.ShioajiClient)
    dummy._quote_dispatch_async = True
    dummy._quote_dispatch_queue_size = 1
    import queue

    dummy._quote_dispatch_queue = queue.Queue(maxsize=1)
    dummy._quote_dispatch_queue.put_nowait(((), {}))
    dummy._quote_dispatch_running = True
    dummy._quote_dispatch_dropped = 0
    dummy.metrics = None
    dummy._start_quote_dispatch_worker = MagicMock()
    dummy._process_tick = MagicMock()

    mod.ShioajiClient._enqueue_tick(dummy, "Q/TSE/2330")
    assert dummy._quote_dispatch_dropped == 1
    dummy._process_tick.assert_not_called()


def test_dispatch_tick_cb_strict_route_miss_drops(monkeypatch):
    c1 = _DummyClient("c1")
    with mod.CLIENT_REGISTRY_LOCK:
        mod.CLIENT_REGISTRY.clear()
        mod.CLIENT_REGISTRY_BY_CODE.clear()
        mod.CLIENT_REGISTRY_SNAPSHOT = ()
        mod.CLIENT_REGISTRY_BY_CODE_SNAPSHOT = {}
        mod.TOPIC_CODE_CACHE.clear()
    mod._registry_register(c1)
    mod._registry_rebind_codes(c1, ["2330"])

    monkeypatch.setattr(mod, "_ROUTE_MISS_STRICT", True)
    monkeypatch.setattr(mod, "_record_route_metric", lambda kind: None)
    monkeypatch.setattr(mod, "_ROUTE_MISS_COUNT", 0)

    class Quote:
        code = "2317"

    mod.dispatch_tick_cb("Q/TSE/2317", Quote())
    c1._enqueue_tick.assert_not_called()


def test_dispatch_tick_cb_route_miss_falls_back_to_wildcard_only(monkeypatch):
    c_exact = _DummyClient("exact")
    c_other = _DummyClient("other")
    c_wild = _DummyClient("wild")
    c_wild.allow_symbol_fallback = True
    with mod.CLIENT_REGISTRY_LOCK:
        mod.CLIENT_REGISTRY.clear()
        mod.CLIENT_REGISTRY_BY_CODE.clear()
        mod.CLIENT_REGISTRY_SNAPSHOT = ()
        mod.CLIENT_REGISTRY_BY_CODE_SNAPSHOT = {}
        mod.CLIENT_REGISTRY_WILDCARD_SNAPSHOT = ()
        mod.CLIENT_DISPATCH_SNAPSHOT = ()
        mod.CLIENT_DISPATCH_BY_CODE_SNAPSHOT = {}
        mod.CLIENT_DISPATCH_WILDCARD_SNAPSHOT = ()
        mod.TOPIC_CODE_CACHE.clear()
    mod._registry_register(c_exact)
    mod._registry_register(c_other)
    mod._registry_register(c_wild)
    mod._registry_rebind_codes(c_exact, ["2330"])
    mod._registry_rebind_codes(c_other, ["2317"])

    monkeypatch.setattr(mod, "_ROUTE_MISS_STRICT", False)
    monkeypatch.setattr(mod, "_ROUTE_MISS_FALLBACK_MODE", "wildcard")
    monkeypatch.setattr(mod, "_record_route_metric", lambda kind: None)
    monkeypatch.setattr(mod, "_ROUTE_MISS_COUNT", 0)

    class Quote:
        code = "9999"

    mod.dispatch_tick_cb("Q/TSE/9999", Quote())

    c_exact._enqueue_tick.assert_not_called()
    c_other._enqueue_tick.assert_not_called()
    c_wild._enqueue_tick.assert_called_once()


def test_registry_unregister_removes_routes():
    c1 = _DummyClient("c1")
    with mod.CLIENT_REGISTRY_LOCK:
        mod.CLIENT_REGISTRY.clear()
        mod.CLIENT_REGISTRY_BY_CODE.clear()
        mod.CLIENT_REGISTRY_SNAPSHOT = ()
        mod.CLIENT_REGISTRY_BY_CODE_SNAPSHOT = {}
        mod.TOPIC_CODE_CACHE.clear()
    mod._registry_register(c1)
    mod._registry_rebind_codes(c1, ["2330"])

    snapshot, routed = mod._registry_snapshot("2330")
    assert routed is True
    assert c1 in snapshot

    mod._registry_unregister(c1)
    snapshot, routed = mod._registry_snapshot("2330")
    assert c1 not in snapshot
    assert routed is False


def test_close_stops_worker_and_unregisters(monkeypatch):
    dummy = object.__new__(mod.ShioajiClient)
    dummy._quote_watchdog_running = True
    dummy._callbacks_retrying = True
    dummy._event_callback_retrying = True
    dummy._resubscribe_scheduled = True
    dummy._pending_quote_relogining = True
    dummy.tick_callback = MagicMock()
    dummy.api = None
    dummy._stop_quote_dispatch_worker = MagicMock()

    unreg = MagicMock()
    monkeypatch.setattr(mod, "_registry_unregister", unreg)

    mod.ShioajiClient.close(dummy)

    dummy._stop_quote_dispatch_worker.assert_called_once()
    unreg.assert_called_once_with(dummy)
    assert dummy.tick_callback is None
