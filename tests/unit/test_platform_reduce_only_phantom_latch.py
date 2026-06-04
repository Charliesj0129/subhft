"""Regression tests for the phantom reduce-only latch (THESHOW 2026-06-04).

Root cause: a transient, *auto-recoverable* reason (``feed_reconnect_pending``
during the 2026-06-03 upgrade cutover) entered ``PLATFORM_REDUCE_ONLY`` with
``manual_rearm_required=True`` and persisted that flag to ``runtime_state.json``.
The flag was never cleared when the reason auto-recovered, so every restart
restored it via the non-auto-recoverable ``restored_from_runtime_state``
sentinel — a self-perpetuating latch that flooded Telegram with
``PlatformReduceOnlyActive`` + ``ManualRearmRequired`` for ~28h with no real
unsafe condition.

Invariant being locked in: an auto-recoverable reason must NEVER be treated as
a manual-rearm condition — in the persisted flag, in the metric, or on restore.
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


def _controller(tmp_path: Path) -> tuple[PlatformDegradeController, AutonomyEvidenceWriter]:
    writer = AutonomyEvidenceWriter(base_dir=tmp_path)
    ctrl = PlatformDegradeController(metrics=None, evidence_writer=writer)
    return ctrl, writer


class TestAutoRecoverableReasonIsNotManualRearm:
    def test_auto_recoverable_reason_does_not_require_manual_rearm(self, tmp_path: Path) -> None:
        ctrl, _ = _controller(tmp_path)
        transition = ctrl.enter_reduce_only(reason="feed_reconnect_pending")
        assert ctrl.reduce_only_active is True
        assert transition.manual_rearm_required is False
        assert ctrl.manual_rearm_required_active is False

    def test_non_recoverable_reason_requires_manual_rearm(self, tmp_path: Path) -> None:
        ctrl, _ = _controller(tmp_path)
        transition = ctrl.enter_reduce_only(reason="pnl_peak_drawdown")
        assert ctrl.reduce_only_active is True
        assert transition.manual_rearm_required is True
        assert ctrl.manual_rearm_required_active is True

    def test_auto_recoverable_reason_is_not_persisted_as_manual_rearm(self, tmp_path: Path) -> None:
        """The core Bug #1 regression: a transient auto-recoverable reason must
        not write ``manual_rearm_required: True`` to runtime_state.json."""
        ctrl, _ = _controller(tmp_path)
        ctrl.enter_reduce_only(reason="feed_reconnect_pending")
        state_path = tmp_path / "runtime_state.json"
        if state_path.exists():
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            assert persisted["platform"]["manual_rearm_required"] is False

    def test_non_recoverable_reason_is_persisted_as_manual_rearm(self, tmp_path: Path) -> None:
        ctrl, _ = _controller(tmp_path)
        ctrl.enter_reduce_only(reason="pnl_peak_drawdown")
        persisted = json.loads((tmp_path / "runtime_state.json").read_text(encoding="utf-8"))
        assert persisted["platform"]["manual_rearm_required"] is True
        assert persisted["platform"]["reason"] == "pnl_peak_drawdown"


class TestRestoreDoesNotPerpetuatePhantomLatch:
    def setup_method(self) -> None:
        reset_shared_platform_degrade_controller()

    def teardown_method(self) -> None:
        reset_shared_platform_degrade_controller()

    def test_stale_auto_recoverable_persisted_flag_is_cleared_not_relatched(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Exactly the production scenario: legacy/buggy persisted state holds an
        auto-recoverable reason with manual_rearm_required=true. On restart the
        controller must NOT re-latch reduce_only; it must clear the stale flag so
        the platform boots NORMAL and stops re-firing alerts."""
        state_path = tmp_path / "runtime_state.json"
        state_path.write_text(
            '{"platform": {"manual_rearm_required": true, '
            '"reason": "feed_reconnect_pending"}, "strategies": {}}',
            encoding="utf-8",
        )
        monkeypatch.setattr("hft_platform.ops.manual_rearm.DEFAULT_RUNTIME_STATE_PATH", state_path)
        ctrl = get_shared_platform_degrade_controller()
        assert ctrl.reduce_only_active is False
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["platform"]["manual_rearm_required"] is False

    def test_non_recoverable_persisted_flag_relatches_with_original_reason(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A genuine operator/manual condition must survive restart and keep the
        ORIGINAL reason (not the meaningless ``restored_from_runtime_state``)."""
        state_path = tmp_path / "runtime_state.json"
        state_path.write_text(
            '{"platform": {"manual_rearm_required": true, '
            '"reason": "pnl_peak_drawdown"}, "strategies": {}}',
            encoding="utf-8",
        )
        monkeypatch.setattr("hft_platform.ops.manual_rearm.DEFAULT_RUNTIME_STATE_PATH", state_path)
        ctrl = get_shared_platform_degrade_controller()
        assert ctrl.reduce_only_active is True
        assert "pnl_peak_drawdown" in ctrl._active_reasons
        assert "restored_from_runtime_state" not in ctrl._active_reasons
        assert ctrl.manual_rearm_required_active is True

    def test_restored_non_recoverable_reason_blocks_auto_recovery(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        state_path = tmp_path / "runtime_state.json"
        state_path.write_text(
            '{"platform": {"manual_rearm_required": true, '
            '"reason": "pnl_peak_drawdown"}, "strategies": {}}',
            encoding="utf-8",
        )
        monkeypatch.setattr("hft_platform.ops.manual_rearm.DEFAULT_RUNTIME_STATE_PATH", state_path)
        ctrl = get_shared_platform_degrade_controller()
        recovered = ctrl.check_auto_recovery(current_reasons=[], now_ns=10**12)
        assert recovered is False
        assert ctrl.reduce_only_active is True
