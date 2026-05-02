"""Coverage tests for ops/position_flattener.py — uncovered paths."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.ops.position_flattener import FlattenResult, PositionFlattener

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(positions_dict=None, open_positions=None):
    """Create a mock position store."""
    store = MagicMock()
    if open_positions is not None:
        store.get_open_positions.return_value = open_positions
    elif positions_dict is not None:
        if hasattr(store, "get_open_positions"):
            del store.get_open_positions
        store.positions = {}
        for key, data in positions_dict.items():
            pos = SimpleNamespace(symbol=data["symbol"], net_qty=data["net_qty"])
            store.positions[key] = pos
    else:
        store.get_open_positions.return_value = {}
    return store


def _make_adapter():
    adapter = MagicMock()
    adapter.submit_intent = AsyncMock()
    adapter.cancel_all_for_symbols = AsyncMock()
    adapter.cancel_all = AsyncMock()
    return adapter


# ---------------------------------------------------------------------------
# FlattenResult
# ---------------------------------------------------------------------------


class TestFlattenResult:
    def test_default_values(self):
        r = FlattenResult()
        assert r.submitted == 0
        assert r.partially_closed == 0
        assert r.failed == 0
        assert r.failed_symbols == []
        assert r.fully_closed == 0  # alias for submitted

    def test_fully_closed_alias(self):
        r = FlattenResult(submitted=5)
        assert r.fully_closed == 5


# ---------------------------------------------------------------------------
# PositionFlattener.flatten_all
# ---------------------------------------------------------------------------


class TestFlattenAll:
    @pytest.mark.asyncio
    async def test_flatten_all_no_positions(self):
        store = _make_store(open_positions={})
        adapter = _make_adapter()
        flattener = PositionFlattener(store, adapter)
        result = await flattener.flatten_all()
        assert result.submitted == 0
        assert result.failed == 0

    @pytest.mark.asyncio
    async def test_flatten_all_closes_long_positions(self):
        store = _make_store(open_positions={"SYM1": 5, "SYM2": -3})
        adapter = _make_adapter()
        flattener = PositionFlattener(store, adapter)
        result = await flattener.flatten_all()
        assert result.submitted == 2
        assert adapter.submit_intent.call_count == 2

    @pytest.mark.asyncio
    async def test_flatten_all_filters_zero_positions(self):
        store = _make_store(open_positions={"SYM1": 5, "SYM2": 0})
        adapter = _make_adapter()
        flattener = PositionFlattener(store, adapter)
        result = await flattener.flatten_all()
        # SYM2 qty=0 is filtered by _get_open_positions
        assert result.submitted == 1
        assert adapter.submit_intent.call_count == 1

    @pytest.mark.asyncio
    async def test_flatten_all_submit_failure_retries(self):
        store = _make_store(open_positions={"SYM1": 5})
        adapter = _make_adapter()
        # First call fails, retry succeeds
        adapter.submit_intent.side_effect = [RuntimeError("fail"), None]
        flattener = PositionFlattener(store, adapter)
        result = await flattener.flatten_all()
        assert result.partially_closed == 1
        assert adapter.submit_intent.call_count == 2

    @pytest.mark.asyncio
    async def test_flatten_all_both_attempts_fail(self):
        store = _make_store(open_positions={"SYM1": 5})
        adapter = _make_adapter()
        adapter.submit_intent.side_effect = RuntimeError("always fail")
        flattener = PositionFlattener(store, adapter)
        result = await flattener.flatten_all()
        assert result.failed == 1
        assert "SYM1" in result.failed_symbols


# ---------------------------------------------------------------------------
# PositionFlattener.flatten_track
# ---------------------------------------------------------------------------


class TestFlattenTrack:
    @pytest.mark.asyncio
    async def test_flatten_track_filters_symbols(self):
        store = _make_store(open_positions={"SYM1": 5, "SYM2": -3, "SYM3": 2})
        adapter = _make_adapter()
        flattener = PositionFlattener(store, adapter)
        result = await flattener.flatten_track("track1", ["SYM1", "SYM3"])
        assert result.submitted == 2

    @pytest.mark.asyncio
    async def test_flatten_track_no_matching_symbols(self):
        store = _make_store(open_positions={"SYM1": 5})
        adapter = _make_adapter()
        flattener = PositionFlattener(store, adapter)
        result = await flattener.flatten_track("track1", ["SYM_OTHER"])
        assert result.submitted == 0


# ---------------------------------------------------------------------------
# PositionFlattener.flatten_strategy
# ---------------------------------------------------------------------------


class TestFlattenStrategy:
    @pytest.mark.asyncio
    async def test_flatten_strategy_flattens_all(self):
        store = _make_store(open_positions={"SYM1": 5})
        adapter = _make_adapter()
        flattener = PositionFlattener(store, adapter)
        result = await flattener.flatten_strategy("strat1")
        assert result.submitted == 1


# ---------------------------------------------------------------------------
# _get_open_positions fallback via positions dict
# ---------------------------------------------------------------------------


class TestGetOpenPositionsFallback:
    def test_fallback_via_positions_dict(self):
        store = MagicMock(spec=[])
        store.positions = {
            "key1": SimpleNamespace(symbol="SYM1", net_qty=5),
            "key2": SimpleNamespace(symbol="SYM2", net_qty=0),
        }
        adapter = _make_adapter()
        flattener = PositionFlattener(store, adapter)
        positions = flattener._get_open_positions()
        assert "SYM1" in positions
        assert positions["SYM1"] == 5
        assert "SYM2" not in positions  # zero qty filtered

    def test_fallback_no_positions_attr(self):
        store = MagicMock(spec=[])
        adapter = _make_adapter()
        flattener = PositionFlattener(store, adapter)
        positions = flattener._get_open_positions()
        assert positions == {}

    def test_multiple_positions_same_symbol_aggregated(self):
        store = MagicMock(spec=[])
        store.positions = {
            "key1": SimpleNamespace(symbol="SYM1", net_qty=3),
            "key2": SimpleNamespace(symbol="SYM1", net_qty=2),
        }
        adapter = _make_adapter()
        flattener = PositionFlattener(store, adapter)
        positions = flattener._get_open_positions()
        assert positions["SYM1"] == 5


# ---------------------------------------------------------------------------
# _cancel_pending
# ---------------------------------------------------------------------------


class TestCancelPending:
    @pytest.mark.asyncio
    async def test_cancel_all_for_symbols(self):
        adapter = _make_adapter()
        store = _make_store()
        flattener = PositionFlattener(store, adapter)
        await flattener._cancel_pending(["SYM1", "SYM2"])
        adapter.cancel_all_for_symbols.assert_called_once_with(["SYM1", "SYM2"])

    @pytest.mark.asyncio
    async def test_cancel_fallback_to_cancel_all(self):
        adapter = MagicMock(spec=[])
        adapter.cancel_all = AsyncMock()
        store = _make_store()
        flattener = PositionFlattener(store, adapter)
        await flattener._cancel_pending(["SYM1"])
        adapter.cancel_all.assert_called_once()


# ---------------------------------------------------------------------------
# _submit_intent
# ---------------------------------------------------------------------------


class TestSubmitIntent:
    @pytest.mark.asyncio
    async def test_submit_via_submit_intent(self):
        adapter = _make_adapter()
        store = _make_store()
        flattener = PositionFlattener(store, adapter)
        intent = MagicMock()
        await flattener._submit_intent(intent)
        adapter.submit_intent.assert_called_once_with(intent)

    @pytest.mark.asyncio
    async def test_submit_raises_when_no_method(self):
        adapter = MagicMock(spec=[])
        store = _make_store()
        flattener = PositionFlattener(store, adapter)
        with pytest.raises(RuntimeError, match="no submit_intent"):
            await flattener._submit_intent(MagicMock())
