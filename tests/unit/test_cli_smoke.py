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
            patch("os.makedirs"),
            patch("builtins.open", unittest.mock.mock_open()),
            patch("hft_platform.cli._safe_write"),
        ):
            args = MagicMock()
            args.base_dir = "."
            cmd_init(args)

    def test_check_command(self):
        with (
            patch("os.path.exists", return_value=True),
            patch("builtins.open", unittest.mock.mock_open()),
            patch("hft_platform.cli._safe_write"),
        ):
            args = MagicMock()
            args.export = "json"
            cmd_check(args)

    def test_run_command_mocked(self):
        # We need to handle the local import of prometheus_client
        # We can patch exception if import fails, or patch the module if it exists

        # Mock sys.modules to inject prometheus_client mock
        mock_prom_mod = MagicMock()
        mock_start_http = MagicMock()
        mock_prom_mod.start_http_server = mock_start_http

        with patch.dict(sys.modules, {"prometheus_client": mock_prom_mod}):
            with patch("hft_platform.main.HFTSystem") as mock_sys_cls:
                mock_sys = mock_sys_cls.return_value

                async def async_run():
                    pass

                mock_sys.run.side_effect = async_run

                args = MagicMock()
                args.mode = "sim"
                args.simulation = True
                args.symbols = ["2330"]
                args.strategy = "demo"

                # Mock load_settings to return valid settings
                # cmd_run calls load_settings
                with patch(
                    "hft_platform.cli.load_settings", return_value=({"mode": "sim", "prometheus_port": 9090}, {})
                ):
                    cmd_run(args)

                mock_sys_cls.assert_called()
                mock_start_http.assert_called()

    def test_main_dispatch(self):
        with patch("hft_platform.cli.cmd_init") as mock_init:
            with patch.object(sys, "argv", ["hft", "init"]):
                ret = main()
                mock_init.assert_called()
                self.assertEqual(ret, 0)
