"""Unit tests for LOBEngine stale symbol eviction."""

from unittest.mock import patch

from hft_platform.feed_adapter.lob_engine import LOBEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ONE_MINUTE_NS = 60_000_000_000
_ONE_HOUR_NS = 3_600_000_000_000
_TWO_HOURS_NS = 7_200_000_000_000

# Fake "now" base used across tests
_BASE_NOW_NS = 1_000_000_000_000_000_000  # arbitrary large value


def _make_engine(ttl_s: int = 3600) -> LOBEngine:
    """Return a LOBEngine with a controlled TTL."""
    with patch.dict("os.environ", {"HFT_LOB_SYMBOL_TTL_S": str(ttl_s)}):
        engine = LOBEngine()
    return engine


def _add_book(engine: LOBEngine, symbol: str, exch_ts: int) -> None:
    """Add a book entry and manually set its exch_ts."""
    book = engine.get_book(symbol)
    assert book is not None
    book.exch_ts = exch_ts


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEvictStaleSymbolsRemovesOld:
    """Stale books (exch_ts older than TTL) must be evicted."""

    def test_evict_stale_symbols_removes_old(self):
        engine = _make_engine(ttl_s=3600)

        old_ts = _BASE_NOW_NS - _TWO_HOURS_NS  # 2 hours ago → stale
        _add_book(engine, "OLD1", old_ts)
        _add_book(engine, "OLD2", old_ts)

        assert len(engine.books) == 2

        # Advance _eviction_last_run_ns far enough into the past so the rate
        # limiter allows the scan.
        engine._eviction_last_run_ns = _BASE_NOW_NS - _ONE_MINUTE_NS - 1

        with patch(
            "hft_platform.feed_adapter.lob_engine.timebase.now_ns",
            return_value=_BASE_NOW_NS,
        ):
            evicted = engine.evict_stale_symbols()

        assert evicted == 2
        assert "OLD1" not in engine.books
        assert "OLD2" not in engine.books


class TestEvictStaleSymbolsKeepsFresh:
    """Books with recent exch_ts must not be evicted."""

    def test_evict_stale_symbols_keeps_fresh(self):
        engine = _make_engine(ttl_s=3600)

        fresh_ts = _BASE_NOW_NS - 1_000  # 1 µs ago → fresh
        _add_book(engine, "FRESH", fresh_ts)

        engine._eviction_last_run_ns = _BASE_NOW_NS - _ONE_MINUTE_NS - 1

        with patch(
            "hft_platform.feed_adapter.lob_engine.timebase.now_ns",
            return_value=_BASE_NOW_NS,
        ):
            evicted = engine.evict_stale_symbols()

        assert evicted == 0
        assert "FRESH" in engine.books


class TestEvictStaleSymbolsRateLimited:
    """Second call within 60 s must be a no-op."""

    def test_evict_stale_symbols_rate_limited(self):
        engine = _make_engine(ttl_s=3600)

        old_ts = _BASE_NOW_NS - _TWO_HOURS_NS
        _add_book(engine, "STALE", old_ts)

        # First call — rate-limiter allows it (last run is 0)
        engine._eviction_last_run_ns = 0

        with patch(
            "hft_platform.feed_adapter.lob_engine.timebase.now_ns",
            return_value=_BASE_NOW_NS,
        ):
            first = engine.evict_stale_symbols()

        # Restore the stale book to test the second call
        _add_book(engine, "STALE2", old_ts)

        # Second call — last run was just updated to _BASE_NOW_NS, so it's too
        # soon; should be skipped entirely.
        with patch(
            "hft_platform.feed_adapter.lob_engine.timebase.now_ns",
            return_value=_BASE_NOW_NS + 1,  # only 1 ns later
        ):
            second = engine.evict_stale_symbols()

        assert first == 1  # first call evicted the stale book
        assert second == 0  # second call was rate-limited
        assert "STALE2" in engine.books  # not touched


class TestEvictionClearsLastSymbolCache:
    """When the evicted symbol matches _last_symbol, the cache must be cleared."""

    def test_eviction_clears_last_symbol_cache(self):
        engine = _make_engine(ttl_s=3600)

        old_ts = _BASE_NOW_NS - _TWO_HOURS_NS
        _add_book(engine, "CACHED", old_ts)

        # Simulate the engine having cached this symbol
        engine._last_symbol = "CACHED"
        engine._last_book = engine.books["CACHED"]

        engine._eviction_last_run_ns = 0

        with patch(
            "hft_platform.feed_adapter.lob_engine.timebase.now_ns",
            return_value=_BASE_NOW_NS,
        ):
            evicted = engine.evict_stale_symbols()

        assert evicted == 1
        assert engine._last_symbol is None
        assert engine._last_book is None


class TestEvictionDisabledWhenTtlZero:
    """Setting TTL to 0 must disable eviction entirely."""

    def test_eviction_disabled_when_ttl_zero(self):
        engine = _make_engine(ttl_s=0)
        assert engine._eviction_ttl_ns == 0

        old_ts = _BASE_NOW_NS - _TWO_HOURS_NS
        _add_book(engine, "SHOULD_STAY", old_ts)

        engine._eviction_last_run_ns = 0

        with patch(
            "hft_platform.feed_adapter.lob_engine.timebase.now_ns",
            return_value=_BASE_NOW_NS,
        ):
            evicted = engine.evict_stale_symbols()

        assert evicted == 0
        assert "SHOULD_STAY" in engine.books
