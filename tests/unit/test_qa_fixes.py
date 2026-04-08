import tempfile
from pathlib import Path

import yaml

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


def test_shioaji_client_mode(tmp_path):
    # R7: config/symbols.yaml now exceeds MAX_SUBSCRIPTIONS (200).
    # Create a temporary config with a small symbol list to avoid ValueError.
    config_path = tmp_path / "symbols.yaml"
    config_path.write_text(yaml.dump({"symbols": [{"code": "2330"}]}))

    c = ShioajiClient(config_path=str(config_path))
    # Should default to "simulation" if import fails
    # OR "real" if import succeeds (but api is object).

    assert hasattr(c, "mode")
    assert c.mode in ["real", "simulation"]

    # Check get_positions doesn't crash
    pos = c.get_positions()
    assert isinstance(pos, list)
