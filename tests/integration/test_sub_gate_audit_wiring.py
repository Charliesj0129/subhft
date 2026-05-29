"""Integration: _invoke_sub_gates emits sub_gate_audit JSONL when enabled.

Goal §4 / §9 traceability — Round 9 wires the writer into the Gate-C
orchestration so every invocation is replayable without each caller
opting in by hand.  The wiring is gated on
``HFT_SUB_GATE_AUDIT_ENABLED=1`` to keep existing test fixtures and
loose-profile runs unchanged by default.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hft_platform.alpha import sub_gate_audit
from hft_platform.alpha._gate_c import _invoke_sub_gates
from hft_platform.alpha._validation_profile import ValidationProfile


def _payload(*, run_id: str = "round9_run1", fills: int = 39, days: int = 31) -> dict:
    return {
        "run_id": run_id,
        "config_hash": "abc",
        "instrument": "TMFD6",
        "strategy_name": "r47_maker_tmf",
        "engine": "maker_engine",
        "queue_model": "QueueDepletionFill(qf=0.5)",
        "calibration_profile_id": "uncalibrated",
        "data_source": "ck",
        "latency_profile": "shioaji_measured_p95",
        "pnl_pts": 2253.0,
        "n_fills": fills,
        "n_trading_days": days,
        "equity_curve": None,
        "pnl_per_fill": 61.5,
        "adverse_fill_pct": 0.30,
        "fill_rate_per_day": 1.26,
        "daily_pnl": [2325.0] + [-2.4] * (days - 1 if days > 1 else 0),
    }


def _strict_profile() -> ValidationProfile:
    return ValidationProfile(
        name="round9_strict",
        is_strict=True,
        thresholds={
            "maker": {
                "min_fills": 300,
                "min_days": 60,
                "outlier_day_contribution_max_pct": 25.0,
                "loo_day_sign_preserved": True,
            }
        },
        blocking_sub_gates=("min_sample_size", "single_day_dominance"),
    )


@pytest.fixture
def _isolated_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "sub_gate_runs.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    sub_gate_audit._reset_cache_for_tests()
    return path


class TestSubGateAuditWiring:
    def test_no_record_emitted_when_flag_off(
        self, _isolated_audit: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HFT_SUB_GATE_AUDIT_ENABLED", raising=False)
        prof = _strict_profile()
        _invoke_sub_gates(
            strategy_type="maker",
            result_payload=_payload(),
            thresholds=prof.thresholds_for(strategy_type="maker"),
            profile=prof,
        )
        assert not _isolated_audit.exists()

    def test_strict_invocation_emits_row_when_flag_on(
        self, _isolated_audit: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_SUB_GATE_AUDIT_ENABLED", "1")
        prof = _strict_profile()
        _, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=_payload(),
            thresholds=prof.thresholds_for(strategy_type="maker"),
            profile=prof,
        )
        assert _isolated_audit.exists()
        rows = _isolated_audit.read_text().splitlines()
        assert len(rows) == 1
        row = json.loads(rows[0])
        assert row["run_id"] == "round9_run1"
        assert row["strategy_name"] == "r47_maker_tmf"
        assert row["instrument"] == "TMFD6"
        assert row["strategy_type"] == "maker"
        assert row["profile_name"] == "round9_strict"
        # The R47 fingerprint hits min_sample_size + single_day_dominance,
        # so the wiring must persist the killed verdict — not a sample-*
        # route — verifying triage propagation end-to-end.
        assert row["blocking_passed"] is False
        assert row["triage_status"] == "killed"
        assert "min_sample_size" in row["triage_reasons"]
        # Round 5 metric must round-trip through the writer.
        gates = {g["name"]: g for g in row["sub_gates"]}
        assert "sample_adequacy_label" in gates["min_sample_size"]["metrics"]

    def test_loose_profile_emits_row_with_blocking_null(
        self, _isolated_audit: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_SUB_GATE_AUDIT_ENABLED", "1")
        _invoke_sub_gates(
            strategy_type="maker",
            result_payload=_payload(run_id="round9_loose"),
            thresholds={"sharpe_is_min": 0.5, "winning_day_pct_min": 55},
            profile=None,
        )
        rows = _isolated_audit.read_text().splitlines()
        assert len(rows) == 1
        row = json.loads(rows[0])
        assert row["blocking_passed"] is None
        assert row["triage_status"] == ""
        assert row["profile_name"] == ""

    def test_writer_failure_does_not_break_invocation(
        self,
        _isolated_audit: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force the writer to raise; the gate must still return its
        # advisory + blocking aggregate unchanged.
        monkeypatch.setenv("HFT_SUB_GATE_AUDIT_ENABLED", "1")

        def _boom(*_args: Any, **_kwargs: Any) -> bool:
            raise RuntimeError("disk full")

        monkeypatch.setattr(sub_gate_audit, "record_sub_gate_run", _boom)
        prof = _strict_profile()
        advisory, blocking = _invoke_sub_gates(
            strategy_type="maker",
            result_payload=_payload(),
            thresholds=prof.thresholds_for(strategy_type="maker"),
            profile=prof,
        )
        assert blocking is not None
        assert blocking["triage_status"] == "killed"
        assert len(advisory) > 0

    def test_missing_run_id_skips_emit_silently(
        self, _isolated_audit: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Payloads without run_id (legacy fixtures) must not crash the
        # gate; we just skip the row.
        monkeypatch.setenv("HFT_SUB_GATE_AUDIT_ENABLED", "1")
        payload = _payload()
        payload.pop("run_id")
        prof = _strict_profile()
        _invoke_sub_gates(
            strategy_type="maker",
            result_payload=payload,
            thresholds=prof.thresholds_for(strategy_type="maker"),
            profile=prof,
        )
        assert not _isolated_audit.exists()
