"""H6: pending_fill FIFO ambiguity can cross-attribute fills between strategies.

Root cause: ``resolve_strategy_from_deal`` pops ``pending[(symbol, side)][0]``
even when the list has multiple entries from different strategies. Fills
can then be silently routed to the wrong strategy — which is logged as
``pending_fill_fifo_ambiguous`` but is not blocked.

Fix: opt-in strict mode (env ``HFT_PENDING_FIFO_STRICT=1``) returns None
on ambiguity, letting the caller DLQ/UNKNOWN-route the fill rather than
silently misattribute it. Default remains the permissive FIFO pop for
backward compatibility.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hft_platform.order.adapter import OrderAdapter


@pytest.fixture
def tmp_config(tmp_path: Path) -> str:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("rate_limits: {}\ncircuit_breaker: {}\n")
    return str(cfg)


def _make_adapter(tmp_config: str) -> OrderAdapter:
    client = MagicMock()
    client.place_order = MagicMock(return_value=MagicMock())
    client.cancel_order = MagicMock(return_value=MagicMock())
    client.mode = "simulation"
    client.activate_ca = False
    q: asyncio.Queue = asyncio.Queue(maxsize=16)
    adapter = OrderAdapter(
        config_path=tmp_config,
        order_queue=q,
        broker_client=client,
    )
    adapter.shadow_sink.enabled = False
    return adapter


def _seed_pending(adapter: OrderAdapter, *entries: tuple[str, str, str]) -> None:
    """Insert (symbol, side, order_key) entries into the pending_fill_index."""
    now = time.monotonic()
    with adapter._pending_fill_lock:
        for symbol, side, order_key in entries:
            key = f"{symbol}:{side}"
            adapter._pending_fill_index.setdefault(key, []).append(order_key)
            adapter._pending_fill_registered_at[order_key] = now


def test_ambiguous_fifo_returns_none_in_strict_mode(tmp_config: str):
    adapter = _make_adapter(tmp_config)
    # The flag is read from env at init; set it directly for test determinism.
    adapter._pending_fifo_strict = True
    _seed_pending(
        adapter,
        ("TMFD6", "BUY", "S1:o1"),
        ("TMFD6", "BUY", "S2:o2"),  # second, same (symbol, side) — ambiguous
    )
    resolved = adapter.resolve_strategy_from_deal("TMFD6", "buy")
    assert resolved is None
    # The ambiguous entries must remain — strict mode refuses to pop.
    with adapter._pending_fill_lock:
        assert len(adapter._pending_fill_index.get("TMFD6:BUY", [])) == 2


def test_single_pending_still_resolves_in_strict_mode(tmp_config: str):
    adapter = _make_adapter(tmp_config)
    adapter._pending_fifo_strict = True
    _seed_pending(adapter, ("TMFD6", "BUY", "S1:o1"))
    resolved = adapter.resolve_strategy_from_deal("TMFD6", "buy")
    assert resolved == "S1"


def test_permissive_mode_preserves_legacy_fifo_pop(tmp_config: str):
    adapter = _make_adapter(tmp_config)
    adapter._pending_fifo_strict = False
    _seed_pending(
        adapter,
        ("TMFD6", "BUY", "S1:o1"),
        ("TMFD6", "BUY", "S2:o2"),
    )
    resolved = adapter.resolve_strategy_from_deal("TMFD6", "buy")
    assert resolved == "S1"  # FIFO head
