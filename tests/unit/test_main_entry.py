import asyncio
import sys
import unittest
from unittest.mock import MagicMock, patch

import hft_platform.main


class TestMainEntry(unittest.IsolatedAsyncioTestCase):
    async def test_main_startup(self):
        """Smoke test main entry point."""

        with patch("hft_platform.main.start_resilient_metrics_server") as mock_start:
            with patch("hft_platform.main.HFTSystem") as MockSys:
                mock_inst = MockSys.return_value
                mock_inst.run = MagicMock(side_effect=lambda: asyncio.sleep(0))

                with patch.object(sys, "argv", ["prog"]):
                    await hft_platform.main.main()

                    mock_start.assert_called_once()
                    MockSys.assert_called_once()
