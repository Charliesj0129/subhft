"""A manual reason that arrives during an auto reduce-only must reach the state file.

``_enter_reduce_only_locally`` wrote ``runtime_state.json`` only on the
NORMAL -> REDUCE_ONLY edge. When an auto-recoverable reason already held
reduce-only (recorder DEGRADED -> ``clickhouse_unhealthy``) and a manual one
followed (recorder DATA_LOSS -> ``recorder_data_loss``), the manual latch lived
in memory only: the gauge stayed 0, ``ManualRearmRequired`` never fired, and a
restart booted NORMAL with the data loss forgotten.
"""

from __future__ import annotations

import json
from pathlib import Path

from hft_platform.ops.evidence import AutonomyEvidenceWriter
from hft_platform.ops.platform_degrade import (
    PlatformDegradeController,
    get_shared_platform_degrade_controller,
    reset_shared_platform_degrade_controller,
)


def _persisted(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "runtime_state.json").read_text(encoding="utf-8"))["platform"]


def _escalated(tmp_path: Path) -> PlatformDegradeController:
    ctrl = PlatformDegradeController(metrics=None, evidence_writer=AutonomyEvidenceWriter(base_dir=tmp_path))
    ctrl.enter_reduce_only(reason="clickhouse_unhealthy")
    ctrl.enter_reduce_only(reason="recorder_data_loss")
    return ctrl


def test_manual_reason_after_auto_reduce_only_is_persisted_as_manual(tmp_path: Path) -> None:
    ctrl = _escalated(tmp_path)

    assert ctrl.manual_rearm_required_active is True
    assert ctrl.last_transition.manual_rearm_required is True
    assert _persisted(tmp_path)["manual_rearm_required"] is True
    assert _persisted(tmp_path)["reason"] == "recorder_data_loss"


def test_a_repeated_manual_reason_writes_only_once(tmp_path: Path, monkeypatch) -> None:
    ctrl = _escalated(tmp_path)
    writes: list[str] = []
    monkeypatch.setattr(ctrl, "_pending_write", lambda t, m: lambda: writes.append(t.reason))

    ctrl.enter_reduce_only(reason="recorder_data_loss")  # re-emitted next tick
    ctrl.enter_reduce_only(reason="clickhouse_unhealthy")

    assert writes == []


def test_an_escalated_latch_survives_a_restart(tmp_path: Path, monkeypatch) -> None:
    _escalated(tmp_path)
    reset_shared_platform_degrade_controller()
    monkeypatch.setattr("hft_platform.ops.manual_rearm.DEFAULT_RUNTIME_STATE_PATH", tmp_path / "runtime_state.json")
    try:
        restored = get_shared_platform_degrade_controller()
        assert restored.reduce_only_active is True
        assert "recorder_data_loss" in restored._active_reasons
        assert restored.manual_rearm_required_active is True
    finally:
        reset_shared_platform_degrade_controller()
