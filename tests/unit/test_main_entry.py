import asyncio
import sys
import unittest
from unittest.mock import MagicMock, patch

# Import main module to patch where necessary
import hft_platform.main


class TestMainEntry(unittest.IsolatedAsyncioTestCase):
    async def test_main_startup(self):
        """Smoke test main entry point."""
        # We need to patch HFTSystem inside 'hft_platform.main'
        # But if it was already imported, we might need to patch the object itself
        # or reload. The simplest robust way for this specific test:

        with patch("hft_platform.main.HFTSystem") as MockSys:
            mock_inst = MockSys.return_value
            mock_inst.run = MagicMock(side_effect=lambda: asyncio.sleep(0))

            with patch.object(sys, "argv", ["prog"]):
                await hft_platform.main.main()

                # Verify
                MockSys.assert_called_once()
