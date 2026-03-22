"""Tests for concrete callback signature and lock-free topic cache in router."""

from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.feed_adapter.shioaji import router as mod


class _DummyClient:
    def __init__(self, name: str):
        self.name = name
        self.allow_symbol_fallback = False
        self._enqueue_tick = MagicMock()


def _reset_registry() -> None:
    """Reset all global router state for test isolation."""
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
        mod._CACHE_WARMED = False


# --- Concrete 2-arg callback tests ---


class TestConcreteCallback:
    def setup_method(self) -> None:
        _reset_registry()

    def test_dispatch_routes_by_code_with_topic_and_quote(self) -> None:
        c1 = _DummyClient("c1")
        c2 = _DummyClient("c2")
        mod._registry_register(c1)
        mod._registry_register(c2)
        mod._registry_rebind_codes(c1, ["2330"])
        mod._registry_rebind_codes(c2, ["2317"])

        class Quote:
            code = "2330"

        q = Quote()
        mod.dispatch_tick_cb("Q/TSE/2330", q)

        c1._enqueue_tick.assert_called_once_with("Q/TSE/2330", q)
        c2._enqueue_tick.assert_not_called()

    def test_dispatch_extracts_code_from_dict_quote(self) -> None:
        c1 = _DummyClient("c1")
        mod._registry_register(c1)
        mod._registry_rebind_codes(c1, ["2330"])

        quote = {"code": "2330", "price": 100}
        mod.dispatch_tick_cb("Q/TSE/2330", quote)

        c1._enqueue_tick.assert_called_once_with("Q/TSE/2330", quote)

    def test_dispatch_falls_back_to_topic_when_quote_has_no_code(self) -> None:
        c1 = _DummyClient("c1")
        mod._registry_register(c1)
        mod._registry_rebind_codes(c1, ["2330"])

        quote = {"price": 100}  # no code field
        mod.dispatch_tick_cb("Q/TSE/2330", quote)

        c1._enqueue_tick.assert_called_once_with("Q/TSE/2330", quote)

    def test_dispatch_handles_none_quote(self) -> None:
        c1 = _DummyClient("c1")
        mod._registry_register(c1)
        mod._registry_rebind_codes(c1, ["2330"])

        mod.dispatch_tick_cb("Q/TSE/2330", None)

        c1._enqueue_tick.assert_called_once_with("Q/TSE/2330", None)

    def test_dispatch_with_l1_topic_format(self) -> None:
        c1 = _DummyClient("c1")
        mod._registry_register(c1)
        mod._registry_rebind_codes(c1, ["2330"])

        class Quote:
            code = "2330"

        q = Quote()
        mod.dispatch_tick_cb("L1:STK:2330:tick", q)

        c1._enqueue_tick.assert_called_once_with("L1:STK:2330:tick", q)

    def test_dispatch_error_does_not_propagate(self) -> None:
        """Exceptions inside dispatch are caught and logged, not raised."""
        c1 = _DummyClient("c1")
        c1._enqueue_tick.side_effect = RuntimeError("boom")
        mod._registry_register(c1)
        mod._registry_rebind_codes(c1, ["2330"])

        class Quote:
            code = "2330"

        # Should not raise — exception is caught and logged internally.
        mod.dispatch_tick_cb("Q/TSE/2330", Quote())
        assert c1._enqueue_tick.call_count == 1


# --- Compat callback tests ---


class TestCompatCallback:
    def setup_method(self) -> None:
        _reset_registry()

    def test_compat_routes_same_as_concrete(self) -> None:
        c1 = _DummyClient("c1")
        mod._registry_register(c1)
        mod._registry_rebind_codes(c1, ["2330"])

        class Quote:
            code = "2330"

        q = Quote()
        mod.dispatch_tick_cb_compat("Q/TSE/2330", q)

        c1._enqueue_tick.assert_called_once_with("Q/TSE/2330", q)

    def test_compat_empty_args_returns_early(self) -> None:
        """No args/kwargs should be a no-op."""
        mod.dispatch_tick_cb_compat()
        assert mod.CLIENT_REGISTRY_SNAPSHOT == ()


# --- Cache warmup tests ---


class TestCacheWarmup:
    def setup_method(self) -> None:
        _reset_registry()

    def test_rebind_sets_cache_warmed(self) -> None:
        c1 = _DummyClient("c1")
        mod._registry_register(c1)
        assert mod._CACHE_WARMED is False

        mod._registry_rebind_codes(c1, ["2330"])
        assert mod._CACHE_WARMED is True

    def test_warmup_prepopulates_common_topics(self) -> None:
        c1 = _DummyClient("c1")
        mod._registry_register(c1)
        mod._registry_rebind_codes(c1, ["2330", "2317"])

        assert mod.TOPIC_CODE_CACHE.get("Q/TSE/2330") == "2330"
        assert mod.TOPIC_CODE_CACHE.get("Q/OTC/2330") == "2330"
        assert mod.TOPIC_CODE_CACHE.get("L1:STK:2330:tick") == "2330"
        assert mod.TOPIC_CODE_CACHE.get("Q/TSE/2317") == "2317"
        assert mod.TOPIC_CODE_CACHE.get("Q/OTC/2317") == "2317"
        assert mod.TOPIC_CODE_CACHE.get("L1:STK:2317:tick") == "2317"

    def test_unregister_resets_cache_warmed(self) -> None:
        c1 = _DummyClient("c1")
        mod._registry_register(c1)
        mod._registry_rebind_codes(c1, ["2330"])
        assert mod._CACHE_WARMED is True

        mod._registry_unregister(c1)
        assert mod._CACHE_WARMED is False

    def test_lock_free_reads_after_warmup(self) -> None:
        """After warmup, cache hits skip the lock entirely."""
        c1 = _DummyClient("c1")
        mod._registry_register(c1)
        mod._registry_rebind_codes(c1, ["2330"])
        assert mod._CACHE_WARMED is True

        # Pre-populated topic should be a cache hit — no lock needed.
        result = mod._extract_code_from_topic("Q/TSE/2330")
        assert result == "2330"

    def test_unknown_topic_post_warmup_still_caches(self) -> None:
        """New topics discovered post-warmup are cached without the lock."""
        c1 = _DummyClient("c1")
        mod._registry_register(c1)
        mod._registry_rebind_codes(c1, ["2330"])
        assert mod._CACHE_WARMED is True

        # Clear pre-populated entries to force a miss.
        mod.TOPIC_CODE_CACHE.clear()

        result = mod._extract_code_from_topic("Q/TSE/9999")
        assert result == "9999"
        # Should be cached after first miss.
        assert mod.TOPIC_CODE_CACHE.get("Q/TSE/9999") == "9999"

    def test_cache_max_respected_post_warmup(self, monkeypatch: object) -> None:
        """Post-warmup, cache writes are skipped when at capacity."""
        import hft_platform.feed_adapter.shioaji.router as r

        c1 = _DummyClient("c1")
        mod._registry_register(c1)
        mod._registry_rebind_codes(c1, ["2330"])

        # Simulate near-max cache.
        monkeypatch.setattr(r, "_TOPIC_CODE_CACHE_MAX", 5)  # type: ignore[attr-defined]
        mod.TOPIC_CODE_CACHE.clear()
        for i in range(5):
            mod.TOPIC_CODE_CACHE[f"topic_{i}"] = f"code_{i}"

        # At max — new entry should NOT be added.
        mod._extract_code_from_topic("Q/TSE/NEW_CODE")
        assert "Q/TSE/NEW_CODE" not in mod.TOPIC_CODE_CACHE
