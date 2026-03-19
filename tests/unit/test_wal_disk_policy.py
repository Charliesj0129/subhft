import os
import tempfile
from unittest.mock import MagicMock, patch


class TestWALPolicy:
    def test_default_halt(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HFT_WAL_DISK_PRESSURE_POLICY", None)
            with patch("hft_platform.recorder.wal.MetricsRegistry") as m:
                m.get.return_value = MagicMock()
                import importlib

                import hft_platform.recorder.wal as w

                importlib.reload(w)
                assert w.WALWriter(tempfile.mkdtemp())._disk_pressure_policy == "halt"
