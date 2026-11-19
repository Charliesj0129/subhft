import yaml

from hft_platform.execution.normalizer import ExecutionNormalizer, OrderStatus
from hft_platform.feed_adapter.shioaji_client import ShioajiClient


def test_normalizer_status_mapping():
    n = ExecutionNormalizer()

    assert n._map_status("Submitted") == OrderStatus.SUBMITTED
    assert n._map_status("PreSubmitted") == OrderStatus.SUBMITTED
    assert n._map_status("Filled") == OrderStatus.FILLED
    assert n._map_status("Cancelled") == OrderStatus.CANCELLED

    assert n._map_status("SUBMITTED") == OrderStatus.SUBMITTED

    assert n._map_status("F Pending") == OrderStatus.PENDING_SUBMIT


def test_shioaji_client_mode(tmp_path):
    config_path = tmp_path / "symbols.yaml"
    config_path.write_text(yaml.dump({"symbols": [{"code": "2330"}]}))

    c = ShioajiClient(config_path=str(config_path))

    assert hasattr(c, "mode")
    assert c.mode in ["real", "simulation"]

    pos = c.get_positions()
    if c.mode == "simulation":
        assert pos == []
    else:
        assert pos is None
