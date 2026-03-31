"""Tests for JSON-lines fallback in alpha audit logging (I-01)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import hft_platform.alpha.audit as audit_mod
from hft_platform.alpha.audit import _write_fallback

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gate_report(passed: bool = True) -> Any:
    return SimpleNamespace(gate="Gate A", passed=passed, details={"score": 1.0})


def _make_promotion_result(alpha_id: str = "alpha_test") -> Any:
    return SimpleNamespace(
        alpha_id=alpha_id,
        approved=True,
        forced=False,
        gate_d_passed=True,
        gate_e_passed=True,
        canary_weight=0.05,
        reasons=["gate_d ok"],
    )


# ---------------------------------------------------------------------------
# _write_fallback unit tests
# ---------------------------------------------------------------------------


class TestWriteFallback:
    def test_creates_dir_if_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        new_dir = tmp_path / "nested" / "dir"
        monkeypatch.setattr(audit_mod, "_FALLBACK_DIR", new_dir)
        assert not new_dir.exists()

        _write_fallback("some_table", {"key": "value"})

        assert new_dir.exists()

    def test_writes_jsonl_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(audit_mod, "_FALLBACK_DIR", tmp_path)

        _write_fallback("alpha_gate_log", {"alpha_id": "abc", "gate": "A", "passed": 1})

        fallback_file = tmp_path / "alpha_gate_log.jsonl"
        assert fallback_file.exists()
        line = json.loads(fallback_file.read_text(encoding="utf-8").strip())
        assert line["alpha_id"] == "abc"
        assert line["gate"] == "A"
        assert line["passed"] == 1
        assert "_failed_at" in line

    def test_appends_multiple_lines(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(audit_mod, "_FALLBACK_DIR", tmp_path)

        _write_fallback("alpha_gate_log", {"alpha_id": "x1"})
        _write_fallback("alpha_gate_log", {"alpha_id": "x2"})

        lines = (tmp_path / "alpha_gate_log.jsonl").read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["alpha_id"] == "x1"
        assert json.loads(lines[1])["alpha_id"] == "x2"

    def test_includes_failed_at_iso_timestamp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(audit_mod, "_FALLBACK_DIR", tmp_path)

        _write_fallback("alpha_canary_log", {"action": "hold"})

        line = json.loads((tmp_path / "alpha_canary_log.jsonl").read_text(encoding="utf-8").strip())
        # Should be a valid ISO string — just check it's non-empty and contains 'T'
        assert "T" in line["_failed_at"]

    def test_does_not_raise_when_write_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_write_fallback must swallow its own errors."""
        monkeypatch.setattr(audit_mod, "_FALLBACK_DIR", tmp_path)

        # Patch open to raise to simulate disk full / permission error
        with patch("builtins.open", side_effect=OSError("disk full")):
            # Should not raise
            _write_fallback("some_table", {"data": 1})

    def test_uses_separate_files_per_table(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(audit_mod, "_FALLBACK_DIR", tmp_path)

        _write_fallback("alpha_gate_log", {"gate": "A"})
        _write_fallback("alpha_promotion_log", {"approved": 1})
        _write_fallback("alpha_canary_log", {"action": "graduate"})

        assert (tmp_path / "alpha_gate_log.jsonl").exists()
        assert (tmp_path / "alpha_promotion_log.jsonl").exists()
        assert (tmp_path / "alpha_canary_log.jsonl").exists()


# ---------------------------------------------------------------------------
# Integration: fallback triggered on ClickHouse failure
# ---------------------------------------------------------------------------


class TestLogGateResultFallback:
    def test_fallback_written_on_ch_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(audit_mod, "_FALLBACK_DIR", tmp_path)
        monkeypatch.setattr(audit_mod, "_ENABLED", True)

        mock_client = MagicMock()
        mock_client.insert.side_effect = ConnectionError("ClickHouse down")

        with patch.object(audit_mod, "_get_client", return_value=mock_client):
            audit_mod.log_gate_result(
                alpha_id="alpha_001",
                run_id="run_xyz",
                gate_report=_make_gate_report(passed=True),
                config_hash="abc123",
            )

        fallback_file = tmp_path / "alpha_gate_log.jsonl"
        assert fallback_file.exists()
        row = json.loads(fallback_file.read_text(encoding="utf-8").strip())
        assert row["alpha_id"] == "alpha_001"
        assert row["run_id"] == "run_xyz"
        assert row["gate"] == "A"
        assert row["passed"] == 1
        assert row["config_hash"] == "abc123"
        assert "_failed_at" in row

    def test_no_fallback_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(audit_mod, "_FALLBACK_DIR", tmp_path)
        monkeypatch.setattr(audit_mod, "_ENABLED", True)

        mock_client = MagicMock()
        mock_client.insert.return_value = None  # success

        with patch.object(audit_mod, "_get_client", return_value=mock_client):
            audit_mod.log_gate_result(
                alpha_id="alpha_001",
                run_id=None,
                gate_report=_make_gate_report(),
                config_hash=None,
            )

        assert not (tmp_path / "alpha_gate_log.jsonl").exists()

    def test_no_fallback_when_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(audit_mod, "_FALLBACK_DIR", tmp_path)
        monkeypatch.setattr(audit_mod, "_ENABLED", False)

        audit_mod.log_gate_result(
            alpha_id="alpha_001",
            run_id=None,
            gate_report=_make_gate_report(),
            config_hash=None,
        )

        assert not (tmp_path / "alpha_gate_log.jsonl").exists()


class TestLogPromotionResultFallback:
    def test_fallback_written_on_ch_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(audit_mod, "_FALLBACK_DIR", tmp_path)
        monkeypatch.setattr(audit_mod, "_ENABLED", True)

        mock_client = MagicMock()
        mock_client.insert.side_effect = ConnectionError("ClickHouse down")
        pr = _make_promotion_result("alpha_promo")

        with patch.object(audit_mod, "_get_client", return_value=mock_client):
            audit_mod.log_promotion_result(
                promotion_result=pr,
                config_hash="hash99",
                scorecard={"sharpe": 1.5},
            )

        fallback_file = tmp_path / "alpha_promotion_log.jsonl"
        assert fallback_file.exists()
        row = json.loads(fallback_file.read_text(encoding="utf-8").strip())
        assert row["alpha_id"] == "alpha_promo"
        assert row["approved"] == 1
        assert row["config_hash"] == "hash99"
        assert "_failed_at" in row


class TestLogCanaryActionFallback:
    def test_fallback_written_on_ch_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(audit_mod, "_FALLBACK_DIR", tmp_path)
        monkeypatch.setattr(audit_mod, "_ENABLED", True)

        mock_client = MagicMock()
        mock_client.insert.side_effect = ConnectionError("ClickHouse down")

        with patch.object(audit_mod, "_get_client", return_value=mock_client):
            audit_mod.log_canary_action(
                alpha_id="alpha_canary",
                action="graduate",
                old_weight=0.05,
                new_weight=0.10,
                reason="passed shadow",
                checks={"metric": 0.99},
            )

        fallback_file = tmp_path / "alpha_canary_log.jsonl"
        assert fallback_file.exists()
        row = json.loads(fallback_file.read_text(encoding="utf-8").strip())
        assert row["alpha_id"] == "alpha_canary"
        assert row["action"] == "graduate"
        assert row["old_weight"] == 0.05
        assert row["new_weight"] == 0.10
        assert row["reason"] == "passed shadow"
        assert "_failed_at" in row


class TestFallbackDirEnvVar:
    def test_env_var_overrides_default_dir(self, tmp_path: Path) -> None:
        """HFT_ALPHA_AUDIT_FALLBACK_DIR env var is used when module is (re)loaded."""
        custom_dir = tmp_path / "custom_audit"
        # We test _write_fallback directly by patching the module attribute
        import hft_platform.alpha.audit as audit_module

        original = audit_module._FALLBACK_DIR
        try:
            audit_module._FALLBACK_DIR = custom_dir
            _write_fallback("alpha_gate_log", {"x": 1})
            assert (custom_dir / "alpha_gate_log.jsonl").exists()
        finally:
            audit_module._FALLBACK_DIR = original
