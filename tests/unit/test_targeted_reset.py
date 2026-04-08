from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest


class TestLOBEngineResetBooksForSymbols:
    def _make_engine(self):
        from hft_platform.feed_adapter.lob_engine import BookState, LOBEngine

        engine = LOBEngine()
        for sym in ("A", "B", "C"):
            engine.books[sym] = BookState(sym)
        return engine

    def test_removes_specified_symbols_keeps_others(self):
        from hft_platform.feed_adapter.lob_engine import LOBEngine

        engine = self._make_engine()
        engine.reset_books_for_symbols({"A", "C"})
        assert "A" not in engine.books
        assert "C" not in engine.books
        assert "B" in engine.books

    def test_clears_cache_when_cached_symbol_in_reset_set(self):
        from hft_platform.feed_adapter.lob_engine import LOBEngine

        engine = self._make_engine()
        engine._last_symbol = "A"
        engine._last_book = engine.books["A"]
        engine.reset_books_for_symbols({"A", "C"})
        assert engine._last_symbol is None
        assert engine._last_book is None

    def test_preserves_cache_when_cached_symbol_not_in_reset_set(self):
        from hft_platform.feed_adapter.lob_engine import LOBEngine

        engine = self._make_engine()
        book_b = engine.books["B"]
        engine._last_symbol = "B"
        engine._last_book = book_b
        engine.reset_books_for_symbols({"A", "C"})
        assert engine._last_symbol == "B"
        assert engine._last_book is book_b

    def test_empty_set_removes_nothing(self):
        from hft_platform.feed_adapter.lob_engine import LOBEngine

        engine = self._make_engine()
        engine.reset_books_for_symbols(set())
        assert set(engine.books.keys()) == {"A", "B", "C"}

    def test_symbol_not_present_is_ignored(self):
        from hft_platform.feed_adapter.lob_engine import LOBEngine

        engine = self._make_engine()
        engine.reset_books_for_symbols({"X", "Y"})
        assert set(engine.books.keys()) == {"A", "B", "C"}


class TestFeatureEngineResetSymbols:
    def _make_engine_with_states(self, symbols):
        from hft_platform.feature.engine import FeatureEngine, _FeatureState

        engine = FeatureEngine()
        num_features = len(engine._feature_set.features)
        for sym in symbols:
            engine._states[sym] = _FeatureState(
                seq=0,
                source_ts_ns=0,
                local_ts_ns=0,
                values=tuple(0 for _ in range(num_features)),
                warm_count=0,
            )
            engine._warmup_ready_symbols.add(sym)
        return engine

    def test_resets_specified_symbols_removes_state(self):
        engine = self._make_engine_with_states(["SYM1", "SYM2", "SYM3"])
        engine.reset_symbols({"SYM1", "SYM2"})
        assert "SYM1" not in engine._states
        assert "SYM2" not in engine._states
        assert "SYM3" in engine._states

    def test_empty_set_changes_nothing(self):
        engine = self._make_engine_with_states(["SYM1", "SYM2"])
        engine.reset_symbols(set())
        assert "SYM1" in engine._states
        assert "SYM2" in engine._states

    def test_single_symbol_reset(self):
        engine = self._make_engine_with_states(["ONLY", "OTHER"])
        engine.reset_symbols({"ONLY"})
        assert "ONLY" not in engine._states
        assert "OTHER" in engine._states

    def test_warmup_flags_cleared_for_reset_symbols(self):
        engine = self._make_engine_with_states(["SYM1", "SYM2"])
        engine.reset_symbols({"SYM1"})
        assert "SYM1" not in engine._warmup_ready_symbols
        assert "SYM2" in engine._warmup_ready_symbols
