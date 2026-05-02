import sys
import unittest
from unittest.mock import MagicMock, patch

from hft_platform.cli import cmd_check, cmd_init, cmd_run, main


class TestCLISmoke(unittest.TestCase):
    def test_main_no_args(self):
        # main returns 1 if no args and print_help called
        with patch.object(sys, "argv", ["hft"]), patch("sys.stdout"), patch("sys.stderr"):
            ret = main()
            self.assertEqual(ret, 1)

    def test_init_command(self):
        with (
            patch("os.makedirs") as mock_makedirs,
            patch("builtins.open", unittest.mock.mock_open()),
            patch("hft_platform.cli._safe_write") as mock_write,
        ):
            args = MagicMock()
            args.base_dir = "."
            result = cmd_init(args)
            self.assertIsNone(result)
            mock_makedirs.assert_called()

    def test_check_command(self):
        # Patch load_settings directly to avoid broad builtins.open / os.path.exists
        # patches that interact unpredictably with config loading under cross-test
        # pollution (other tests may alter env vars or module-level state).
        _valid_settings = {"symbols": ["2330"], "strategy": {"id": "demo"}}
        with (
            patch("hft_platform.cli._run.load_settings", return_value=(_valid_settings, {})),
            patch("hft_platform.cli._safe_write") as mock_write,
        ):
            args = MagicMock()
            args.export = "json"
            result = cmd_check(args)
            self.assertIsNone(result)

    def test_run_command_mocked(self):
        with (
            patch("hft_platform.observability.metrics_server.start_resilient_metrics_server") as mock_metrics,
            patch("hft_platform.observability.metrics.MetricsRegistry"),
            patch("hft_platform.main.HFTSystem") as mock_sys_cls,
            patch("hft_platform.cli._run.load_settings", return_value=({"mode": "sim", "prometheus_port": 9090}, {})),
        ):
            mock_sys = mock_sys_cls.return_value

            async def async_run():
                pass

            mock_sys.run.side_effect = async_run

            args = MagicMock()
            args.mode = "sim"
            args.simulation = True
            args.symbols = ["2330"]
            args.strategy = "demo"

            cmd_run(args)

            mock_sys_cls.assert_called()
            mock_metrics.assert_called_once_with(9090)

    def test_main_dispatch(self):
        with patch("hft_platform.cli._run.cmd_init") as mock_init:
            # Re-import parser so argparse binds the mock
            import importlib

            import hft_platform.cli._parser as _parser_mod

            importlib.reload(_parser_mod)
            with patch.object(sys, "argv", ["hft", "init"]):
                ret = main()
                mock_init.assert_called()
                self.assertEqual(ret, 0)
