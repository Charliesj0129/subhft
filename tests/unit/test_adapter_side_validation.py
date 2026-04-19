"""Regression tests for OrderAdapter.resolve_phantom_fill side validation (Bug 16).

Root cause: ``side_name = "SELL" if side is not None and int(side) == 1 else "BUY"``
silently treated ``side=None`` as BUY, causing phantom fills with missing side to
be misattributed to the BUY side of the pending-fill index.

Fix: ``None`` side on a phantom fill event is a data integrity error. Log a warning
and return ``None`` (skip resolution) rather than defaulting.
"""

from __future__ import annotations

from types import SimpleNamespace


def _make_adapter_stub():
    """Construct a minimal OrderAdapter-like object exposing only the fields that
    ``resolve_phantom_fill`` reads. Avoids the full heavy constructor."""
    from hft_platform.order.adapter import OrderAdapter

    stub = OrderAdapter.__new__(OrderAdapter)
    stub._phantom_order_keys = {}
    stub._pending_fill_index = {}
    stub._pending_fill_registered_at = {}
    import threading

    stub._pending_fill_lock = threading.Lock()
    return stub


class TestPhantomFillSideValidation:
    def test_side_none_does_not_default_to_buy(self):
        """Bug 16: side=None must not silently match BUY-side pending fills."""
        adapter = _make_adapter_stub()
        # Plant a pending BUY-side fill; a None-side event must NOT consume it.
        adapter._phantom_order_keys["strat1:abc"] = (0.0, "TMFD6")
        adapter._pending_fill_index["TMFD6:BUY"] = ["strat1:abc"]

        fill = SimpleNamespace(symbol="TMFD6", side=None)
        result = adapter.resolve_phantom_fill(fill)

        assert result is None, "None side must not be resolved via BUY index"
        # Pending index must be untouched
        assert adapter._pending_fill_index.get("TMFD6:BUY") == ["strat1:abc"]

    def test_side_0_maps_to_buy(self):
        adapter = _make_adapter_stub()
        adapter._phantom_order_keys["strat1:abc"] = (0.0, "TMFD6")
        adapter._pending_fill_index["TMFD6:BUY"] = ["strat1:abc"]

        fill = SimpleNamespace(symbol="TMFD6", side=0)
        result = adapter.resolve_phantom_fill(fill)

        assert result == "strat1"
        assert "TMFD6:BUY" not in adapter._pending_fill_index  # consumed

    def test_side_1_maps_to_sell(self):
        adapter = _make_adapter_stub()
        adapter._phantom_order_keys["strat1:xyz"] = (0.0, "TMFD6")
        adapter._pending_fill_index["TMFD6:SELL"] = ["strat1:xyz"]

        fill = SimpleNamespace(symbol="TMFD6", side=1)
        result = adapter.resolve_phantom_fill(fill)

        assert result == "strat1"
        assert "TMFD6:SELL" not in adapter._pending_fill_index  # consumed

    def test_side_none_does_not_match_sell_index_either(self):
        adapter = _make_adapter_stub()
        adapter._phantom_order_keys["strat1:xyz"] = (0.0, "TMFD6")
        adapter._pending_fill_index["TMFD6:SELL"] = ["strat1:xyz"]

        fill = SimpleNamespace(symbol="TMFD6", side=None)
        result = adapter.resolve_phantom_fill(fill)

        assert result is None
        assert adapter._pending_fill_index.get("TMFD6:SELL") == ["strat1:xyz"]
