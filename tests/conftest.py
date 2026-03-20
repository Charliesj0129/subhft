import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

# Add src/ first so compiled hft_platform.rust_core takes priority.
for candidate in (SRC, ROOT):
    path = str(candidate)
    if path not in sys.path:
        sys.path.insert(0, path)

# The rust_core/ directory at ROOT is Cargo source, not a Python package.
# If Python accidentally imported it as a namespace package (before the
# compiled extension was loaded), evict it so the real module can be found.

_rust_ns = sys.modules.get("rust_core")
if _rust_ns is not None and getattr(_rust_ns, "__spec__", None) is not None:
    if getattr(_rust_ns.__spec__, "origin", None) is None:
        # Namespace package — evict so hft_platform.rust_core alias can fill in
        del sys.modules["rust_core"]

# ---------------------------------------------------------------------------
# WU-T01: Shared factory functions and fixtures
# ---------------------------------------------------------------------------
from unittest.mock import MagicMock

import numpy as np
import pytest

from hft_platform.contracts.execution import FillEvent
from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.events import BidAskEvent, MetaData, TickEvent

# Default values (Precision Law: all prices scaled x10000)
_DEFAULT_SYMBOL = "2330"
_DEFAULT_PRICE = 5_000_000  # 500.0 * 10000
_DEFAULT_TS_NS = 1_700_000_000_000_000_000


def make_order_intent(**overrides) -> OrderIntent:
    """Create an OrderIntent with sensible defaults. Override any field via kwargs."""
    defaults = {
        "intent_id": 1,
        "strategy_id": "test_strategy",
        "symbol": _DEFAULT_SYMBOL,
        "intent_type": IntentType.NEW,
        "side": Side.BUY,
        "price": _DEFAULT_PRICE,
        "qty": 1,
        "tif": TIF.LIMIT,
        "target_order_id": None,
        "timestamp_ns": _DEFAULT_TS_NS,
        "source_ts_ns": _DEFAULT_TS_NS,
        "reason": "",
        "trace_id": "",
        "idempotency_key": "",
        "ttl_ns": 0,
    }
    defaults.update(overrides)
    return OrderIntent(**defaults)


def make_fill_event(**overrides) -> FillEvent:
    """Create a FillEvent with sensible defaults. Override any field via kwargs."""
    defaults = {
        "fill_id": "FILL-001",
        "account_id": "ACC-001",
        "order_id": "ORD-001",
        "strategy_id": "test_strategy",
        "symbol": _DEFAULT_SYMBOL,
        "side": Side.BUY,
        "qty": 1,
        "price": _DEFAULT_PRICE,
        "fee": 0,
        "tax": 0,
        "ingest_ts_ns": _DEFAULT_TS_NS,
        "match_ts_ns": _DEFAULT_TS_NS,
    }
    defaults.update(overrides)
    return FillEvent(**defaults)


def make_order_command(**overrides) -> OrderCommand:
    """Create an OrderCommand with sensible defaults. Override any field via kwargs."""
    intent_overrides = overrides.pop("intent", None)
    if intent_overrides is None:
        intent = make_order_intent()
    elif isinstance(intent_overrides, dict):
        intent = make_order_intent(**intent_overrides)
    else:
        # Already an OrderIntent instance
        intent = intent_overrides

    defaults = {
        "cmd_id": 1,
        "intent": intent,
        "deadline_ns": _DEFAULT_TS_NS + 1_000_000_000,  # 1s after default ts
        "storm_guard_state": StormGuardState.NORMAL,
        "created_ns": _DEFAULT_TS_NS,
    }
    defaults.update(overrides)
    return OrderCommand(**defaults)


def make_tick_event(**overrides) -> TickEvent:
    """Create a TickEvent with sensible defaults. Override any field via kwargs."""
    meta_overrides = overrides.pop("meta", None)
    if meta_overrides is None:
        meta = MetaData(seq=1, source_ts=_DEFAULT_TS_NS, local_ts=_DEFAULT_TS_NS)
    elif isinstance(meta_overrides, dict):
        meta_defaults = {"seq": 1, "source_ts": _DEFAULT_TS_NS, "local_ts": _DEFAULT_TS_NS, "topic": ""}
        meta_defaults.update(meta_overrides)
        meta = MetaData(**meta_defaults)
    else:
        meta = meta_overrides

    defaults = {
        "meta": meta,
        "symbol": _DEFAULT_SYMBOL,
        "price": _DEFAULT_PRICE,
        "volume": 100,
        "total_volume": 1000,
        "bid_side_total_vol": 500,
        "ask_side_total_vol": 500,
        "is_simtrade": False,
        "is_odd_lot": False,
    }
    defaults.update(overrides)
    return TickEvent(**defaults)


def make_bidask_event(**overrides) -> BidAskEvent:
    """Create a BidAskEvent with sensible defaults. Override any field via kwargs."""
    meta_overrides = overrides.pop("meta", None)
    if meta_overrides is None:
        meta = MetaData(seq=1, source_ts=_DEFAULT_TS_NS, local_ts=_DEFAULT_TS_NS)
    elif isinstance(meta_overrides, dict):
        meta_defaults = {"seq": 1, "source_ts": _DEFAULT_TS_NS, "local_ts": _DEFAULT_TS_NS, "topic": ""}
        meta_defaults.update(meta_overrides)
        meta = MetaData(**meta_defaults)
    else:
        meta = meta_overrides

    # Default 5-level book: bids descending, asks ascending from default price
    tick_size = 1_000  # 0.1 * 10000
    if "bids" not in overrides:
        bids = np.array(
            [[_DEFAULT_PRICE - i * tick_size, 100] for i in range(5)],
            dtype=np.int64,
        )
    else:
        bids = overrides.pop("bids")

    if "asks" not in overrides:
        asks = np.array(
            [[_DEFAULT_PRICE + (i + 1) * tick_size, 100] for i in range(5)],
            dtype=np.int64,
        )
    else:
        asks = overrides.pop("asks")

    defaults = {
        "meta": meta,
        "symbol": _DEFAULT_SYMBOL,
        "bids": bids,
        "asks": asks,
        "stats": None,
        "fused_stats": None,
        "is_snapshot": False,
    }
    defaults.update(overrides)
    return BidAskEvent(**defaults)


@pytest.fixture()
def mock_metrics() -> MagicMock:
    """Return a MagicMock that can stand in for MetricsRegistry."""
    mock = MagicMock()
    mock.name = "MockMetricsRegistry"
    return mock
