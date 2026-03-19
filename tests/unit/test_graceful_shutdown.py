import asyncio
from unittest.mock import MagicMock, patch


def _make_sys():
    with (
        patch("hft_platform.services.system.SystemBootstrapper") as b,
        patch("hft_platform.services.system.configure_logging"),
    ):
        r = MagicMock()
        r.bus = MagicMock()
        r.raw_queue = asyncio.Queue()
        r.raw_exec_queue = asyncio.Queue()
        r.risk_queue = asyncio.Queue()
        r.order_queue = asyncio.Queue()
        r.recorder_queue = asyncio.Queue()
        r.gateway_service = None
        b.return_value.build.return_value = r
        from hft_platform.services.system import HFTSystem

        return HFTSystem({})


class TestShutdown:
    def test_stop_calls_close(self):
        s = _make_sys()
        s.stop()
        s.md_client.close.assert_called_with(logout=True)
        s.order_client.close.assert_called_with(logout=True)

    def test_handles_close_exception(self):
        s = _make_sys()
        s.md_client.close.side_effect = RuntimeError("err")
        s.stop()
        s.order_client.close.assert_called_with(logout=True)
