"""Unit tests for CanaryMonitor.apply_decision — immutability and atomic write."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from hft_platform.alpha.canary import CanaryMonitor, CanaryStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_canary_yaml(tmp_path: Path, alpha_id: str, weight: float, extra: dict[str, Any] | None = None) -> Path:
    """Write a minimal canary YAML and return its path."""
    payload: dict[str, Any] = {
        "alpha_id": alpha_id,
        "enabled": True,
        "weight": weight,
        "guardrails": {"max_live_slippage_bps": 3.0},
        "rollback": {"trigger": {"live_slippage_bps_gt": 3.0}},
        "scorecard_snapshot": {"sharpe_oos": 1.0},
    }
    if extra:
        payload.update(extra)
    yaml_path = tmp_path / f"{alpha_id}.yaml"
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return yaml_path


def _make_monitor(tmp_path: Path) -> CanaryMonitor:
    return CanaryMonitor(promotions_dir=str(tmp_path))


def _make_status(alpha_id: str, state: str) -> CanaryStatus:
    return CanaryStatus(
        alpha_id=alpha_id,
        current_weight=0.02,
        state=state,
        reason="test",
        checks={},
    )


# ---------------------------------------------------------------------------
# Immutability tests
# ---------------------------------------------------------------------------

class TestApplyDecisionImmutability:
    """Original canary dict returned by _find_canary must not be mutated."""

    def test_rolled_back_does_not_mutate_original(self, tmp_path: Path) -> None:
        _make_canary_yaml(tmp_path, "alpha_rb", weight=0.05)
        monitor = _make_monitor(tmp_path)

        original = monitor._find_canary("alpha_rb")
        assert original is not None
        original_weight_before = original["weight"]
        original_enabled_before = original["enabled"]

        status = _make_status("alpha_rb", "rolled_back")
        monitor.apply_decision(status)

        # original dict must be unchanged
        assert original["weight"] == original_weight_before
        assert original["enabled"] == original_enabled_before

    def test_escalated_does_not_mutate_original(self, tmp_path: Path) -> None:
        _make_canary_yaml(tmp_path, "alpha_esc", weight=0.02)
        monitor = _make_monitor(tmp_path)

        original = monitor._find_canary("alpha_esc")
        assert original is not None
        original_weight_before = original["weight"]

        status = _make_status("alpha_esc", "escalated")
        monitor.apply_decision(status)

        assert original["weight"] == original_weight_before

    def test_graduated_does_not_mutate_original(self, tmp_path: Path) -> None:
        _make_canary_yaml(tmp_path, "alpha_grad", weight=0.07)
        monitor = _make_monitor(tmp_path)

        original = monitor._find_canary("alpha_grad")
        assert original is not None
        original_weight_before = original["weight"]
        original_has_rollback = "rollback" in original

        status = _make_status("alpha_grad", "graduated")
        monitor.apply_decision(status)

        assert original["weight"] == original_weight_before
        assert ("rollback" in original) == original_has_rollback


# ---------------------------------------------------------------------------
# Atomic write tests
# ---------------------------------------------------------------------------

class TestApplyDecisionAtomicWrite:
    """YAML must be written atomically (temp file → rename)."""

    def test_rolled_back_writes_yaml(self, tmp_path: Path) -> None:
        yaml_path = _make_canary_yaml(tmp_path, "alpha_a", weight=0.05)
        monitor = _make_monitor(tmp_path)

        status = _make_status("alpha_a", "rolled_back")
        monitor.apply_decision(status)

        written = yaml.safe_load(yaml_path.read_text())
        assert written["weight"] == 0.0
        assert written["enabled"] is False
        # Internal _path key must NOT be persisted
        assert "_path" not in written

    def test_escalated_writes_next_tier(self, tmp_path: Path) -> None:
        yaml_path = _make_canary_yaml(tmp_path, "alpha_b", weight=0.02)
        monitor = _make_monitor(tmp_path)

        status = _make_status("alpha_b", "escalated")
        monitor.apply_decision(status)

        written = yaml.safe_load(yaml_path.read_text())
        # 0.02 → next tier is 0.05
        assert written["weight"] == pytest.approx(0.05)
        assert "_path" not in written

    def test_graduated_writes_max_tier(self, tmp_path: Path) -> None:
        yaml_path = _make_canary_yaml(tmp_path, "alpha_c", weight=0.07)
        monitor = _make_monitor(tmp_path)

        status = _make_status("alpha_c", "graduated")
        monitor.apply_decision(status)

        written = yaml.safe_load(yaml_path.read_text())
        assert written["weight"] == pytest.approx(0.10)
        assert "rollback" not in written
        assert "_path" not in written

    def test_no_tmp_files_left_behind(self, tmp_path: Path) -> None:
        _make_canary_yaml(tmp_path, "alpha_d", weight=0.02)
        monitor = _make_monitor(tmp_path)

        status = _make_status("alpha_d", "rolled_back")
        monitor.apply_decision(status)

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"Temp files left behind: {tmp_files}"

    def test_atomic_uses_rename(self, tmp_path: Path) -> None:
        """Verify that Path.replace() (rename) is called — the atomic step."""
        _make_canary_yaml(tmp_path, "alpha_e", weight=0.02)
        monitor = _make_monitor(tmp_path)

        replace_calls: list[Any] = []
        original_replace = Path.replace

        def spy_replace(self: Path, target: Any) -> Any:
            replace_calls.append((str(self), str(target)))
            return original_replace(self, target)

        status = _make_status("alpha_e", "rolled_back")
        with patch.object(Path, "replace", spy_replace):
            monitor.apply_decision(status)

        assert len(replace_calls) == 1, "Expected exactly one atomic rename"
        src, dst = replace_calls[0]
        assert src.endswith(".tmp")
        assert not dst.endswith(".tmp")

    def test_cleanup_on_write_error(self, tmp_path: Path) -> None:
        """Temp file is cleaned up when an error occurs during write."""
        _make_canary_yaml(tmp_path, "alpha_f", weight=0.02)
        monitor = _make_monitor(tmp_path)

        status = _make_status("alpha_f", "rolled_back")

        with patch("os.fdopen", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                monitor.apply_decision(status)

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"Temp file not cleaned up: {tmp_files}"


# ---------------------------------------------------------------------------
# No-op / hold state
# ---------------------------------------------------------------------------

class TestApplyDecisionHold:
    def test_hold_state_no_file_change(self, tmp_path: Path) -> None:
        yaml_path = _make_canary_yaml(tmp_path, "alpha_hold", weight=0.02)
        monitor = _make_monitor(tmp_path)

        mtime_before = yaml_path.stat().st_mtime

        status = _make_status("alpha_hold", "canary")  # hold — no-op
        monitor.apply_decision(status)

        assert yaml_path.stat().st_mtime == mtime_before

    def test_unknown_alpha_id_logs_warning(self, tmp_path: Path) -> None:
        monitor = _make_monitor(tmp_path)
        status = _make_status("nonexistent_alpha", "rolled_back")

        # Should not raise; just log a warning
        monitor.apply_decision(status)
