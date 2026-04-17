import pytest

from research.calibration.probe_strategy import PassiveQuoteProbe
from research.calibration.replay import (
    ReplayNotReadyError,
    build_probe_replay_fn,
)
from research.calibration.sweep import QueueModelCandidate


def test_build_probe_replay_fn_returns_callable():
    fn = build_probe_replay_fn(
        instrument="TMFD6",
        probe_factory=lambda: PassiveQuoteProbe(qty=1, max_pos=3),
        l2_data_dir="/nonexistent",
        latency_us=36000,
        tick_size=1.0,
        lot_size=1.0,
    )
    assert callable(fn)


def test_build_probe_replay_fn_missing_data_raises():
    fn = build_probe_replay_fn(
        instrument="TMFD6",
        probe_factory=lambda: PassiveQuoteProbe(qty=1, max_pos=3),
        l2_data_dir="/nonexistent",
        latency_us=36000,
        tick_size=1.0,
        lot_size=1.0,
    )
    cand = QueueModelCandidate("power_prob", 1.5)
    with pytest.raises(FileNotFoundError):
        fn(cand, "2026-03-01")


def test_replay_raises_when_stub_not_allowed(tmp_path):
    """Without allow_stub_execution=True, real execution must raise."""
    data_file = tmp_path / "TMFD6_2026-03-01_l2.hftbt.npz"
    data_file.touch()  # exists but empty; will fail before reading in stub path

    fn = build_probe_replay_fn(
        instrument="TMFD6",
        probe_factory=lambda: PassiveQuoteProbe(qty=1, max_pos=3),
        l2_data_dir=tmp_path,
        latency_us=36000,
        tick_size=1.0,
        lot_size=1.0,
        allow_stub_execution=False,
    )
    cand = QueueModelCandidate("power_prob", 1.5)
    with pytest.raises(ReplayNotReadyError, match="stub"):
        fn(cand, "2026-03-01")
