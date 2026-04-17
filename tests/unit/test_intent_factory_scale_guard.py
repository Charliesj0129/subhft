"""H-3 scale guard: reject under-scaled prices on NEW/AMEND intents.

A strategy that accidentally passes a raw price (e.g. 505 for a stock quoted at
NT$50.5 with price_scale=10000) would emit an intent 10000x smaller than
intended. The runner's intent_factory rejects the intent when the scaled price
falls below the symbol's tick size.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType, Side, TIF
from hft_platform.feed_adapter.normalizer import SymbolMetadata


def _write_symbols(tmp: Path, entries: list[dict]) -> Path:
    import yaml

    path = tmp / "symbols.yaml"
    path.write_text(yaml.safe_dump({"symbols": entries}))
    return path


def _make_runner_with_metadata(metadata: SymbolMetadata):
    from hft_platform.strategy.runner import StrategyRunner

    with patch("hft_platform.strategy.runner.StrategyRegistry") as mock_reg:
        mock_reg.return_value.instantiate.return_value = []
        return StrategyRunner(
            MagicMock(),
            asyncio.Queue(),
            config_path="dummy",
            symbol_metadata=metadata,
        )


def test_intent_factory_rejects_under_scaled_new_order(tmp_path):
    cfg = _write_symbols(
        tmp_path,
        [{"code": "2330", "price_scale": 10_000, "tick_size": 0.5}],
    )
    metadata = SymbolMetadata(config_path=str(cfg))
    runner = _make_runner_with_metadata(metadata)
    runner._typed_intent_fastpath = False  # test non-tuple path for clear attribute access

    with pytest.raises(ValueError, match="under-scaled"):
        runner._intent_factory(
            strategy_id="s1",
            symbol="2330",
            side=Side.BUY,
            price=505,  # should have been 5_050_000
            qty=1,
            tif=TIF.LIMIT,
            intent_type=IntentType.NEW,
        )


def test_intent_factory_accepts_scaled_new_order(tmp_path):
    cfg = _write_symbols(
        tmp_path,
        [{"code": "2330", "price_scale": 10_000, "tick_size": 0.5}],
    )
    metadata = SymbolMetadata(config_path=str(cfg))
    runner = _make_runner_with_metadata(metadata)
    runner._typed_intent_fastpath = False

    intent = runner._intent_factory(
        strategy_id="s1",
        symbol="2330",
        side=Side.BUY,
        price=5_050_000,
        qty=1,
        tif=TIF.LIMIT,
        intent_type=IntentType.NEW,
    )
    assert intent.price == 5_050_000


def test_intent_factory_allows_zero_price_on_cancel(tmp_path):
    cfg = _write_symbols(
        tmp_path,
        [{"code": "2330", "price_scale": 10_000, "tick_size": 0.5}],
    )
    metadata = SymbolMetadata(config_path=str(cfg))
    runner = _make_runner_with_metadata(metadata)
    runner._typed_intent_fastpath = False

    intent = runner._intent_factory(
        strategy_id="s1",
        symbol="2330",
        side=Side.BUY,
        price=0,
        qty=0,
        tif=TIF.LIMIT,
        intent_type=IntentType.CANCEL,
        target_order_id="ORD123",
    )
    assert intent.price == 0


def test_intent_factory_skips_guard_when_no_metadata(tmp_path):
    cfg = _write_symbols(tmp_path, [])
    metadata = SymbolMetadata(config_path=str(cfg))
    runner = _make_runner_with_metadata(metadata)
    runner._typed_intent_fastpath = False

    # No metadata for 'UNKNOWN' → guard is inactive, intent passes.
    intent = runner._intent_factory(
        strategy_id="s1",
        symbol="UNKNOWN",
        side=Side.BUY,
        price=1,
        qty=1,
        tif=TIF.LIMIT,
        intent_type=IntentType.NEW,
    )
    assert intent.price == 1
