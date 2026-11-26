"""The Fubon market-data facade must get a non-empty config, or it never builds its SDK."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.services.bootstrap import SystemBootstrapper


def test_fubon_md_facade_receives_nonempty_config_when_shioaji_cfg_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_ORDER_MODE", "disabled")
    facade_cls = MagicMock()
    fake_module = MagicMock()
    fake_module.FubonClientFacade = facade_cls
    with patch.dict(
        "sys.modules",
        {"hft_platform.feed_adapter.fubon": MagicMock(), "hft_platform.feed_adapter.fubon.facade": fake_module},
    ):
        md, order = SystemBootstrapper(settings={})._build_broker_clients(
            role="engine", symbols_path="config/symbols.yaml", base_shioaji_cfg={}, broker_id="fubon"
        )
    assert facade_cls.call_count == 1
    cfg = facade_cls.call_args.args[1]
    assert cfg, "an empty config makes FubonClientFacade skip building the SDK, so login always fails"
    assert cfg["activate_ca"] is False
    assert order.runtime_role == "order_disabled"
