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
        s.running = True
        s.stop()
        s.md_client.close.assert_called_with(logout=True)
        s.order_client.close.assert_called_with(logout=True)
        assert s.running is False

    def test_handles_close_exception(self):
        s = _make_sys()
        s.md_client.close.side_effect = RuntimeError("err")
        s.stop()
        # order_client.close is still called even though md_client.close raised
        s.order_client.close.assert_called_with(logout=True)

    def test_stop_idempotent(self):
        s = _make_sys()
        s.stop()
        # _teardown_bootstrap called once, sets _bootstrap_torn_down = True
        assert s.bootstrapper.teardown.call_count == 1

        # Second stop: _teardown_bootstrap is a no-op (guarded by flag)
        s.stop()
        assert s.bootstrapper.teardown.call_count == 1

    def test_both_clients_closed_even_when_md_raises(self):
        s = _make_sys()
        s.md_client.close.side_effect = RuntimeError("md exploded")
        s.stop()
        # md_client.close was attempted
        s.md_client.close.assert_called_with(logout=True)
        # order_client.close must still be called despite md_client failure
        s.order_client.close.assert_called_with(logout=True)
