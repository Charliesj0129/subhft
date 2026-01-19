from hft_platform.execution.normalizer import ExecutionNormalizer, OrderStatus
from hft_platform.feed_adapter.shioaji_client import ShioajiClient


def test_normalizer_status_mapping():
    n = ExecutionNormalizer()

    # Test Mixed Case (The Bug)
    assert n._map_status("Submitted") == OrderStatus.SUBMITTED
    assert n._map_status("PreSubmitted") == OrderStatus.SUBMITTED
    assert n._map_status("Filled") == OrderStatus.FILLED
    assert n._map_status("Cancelled") == OrderStatus.CANCELLED

    # Test Upper Case
    assert n._map_status("SUBMITTED") == OrderStatus.SUBMITTED

    # Test Partial Match (if implemented)
    assert n._map_status("F Pending") == OrderStatus.PENDING_SUBMIT


def test_shioaji_client_mode():
    # Test initialization without shioaji installed (Sim Mode)
    # We assume 'sj' is None in test env if not mocked,
    # or if we mock it, we control it.
    # The file has: try: import shioaji ... except: sj = None.
    # We can inspect instance.

    c = ShioajiClient()
    # Should default to "simulation" if import fails
    # OR "real" if import succeeds (but api is object).

    # In this environment, shioaji might be missing in docker env?
    # Let's check attribute existence
    assert hasattr(c, "mode")
    assert c.mode in ["real", "simulation"]

    # Check get_positions doesn't crash
    pos = c.get_positions()
    assert isinstance(pos, list)
