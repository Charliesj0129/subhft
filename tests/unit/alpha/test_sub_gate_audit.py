"""Tests for the per-run sub-gate audit writer (Round 8 / goal §4 §9)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hft_platform.alpha import sub_gate_audit
from hft_platform.alpha.sub_gate_audit import (
    SCHEMA_VERSION,
    build_record,
    read_runs,
    record_sub_gate_run,
)


@pytest.fixture
def _isolated_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "sub_gate_runs.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    sub_gate_audit._reset_cache_for_tests()
    return path


def _advisory() -> list[dict]:
    return [
        {
            "name": "min_sample_size",
            "passed": False,
            "metrics": {"n_fills": 240.0, "sample_adequacy_label": "promising"},
            "details": "fills=240 (min 300), days=50 (min 60), label=promising",
        },
        {
            "name": "edge_per_round_trip",
            "passed": True,
            "metrics": {"mean_net_edge_pts_per_trade": 12.5},
            "details": "edge=12.5 vs min 10.0",
        },
    ]


def _blocking(status: str = "sample_promising") -> dict:
    return {
        "passed": False,
        "failing": [{"name": "min_sample_size", "passed": False, "metrics": {}, "details": ""}],
        "names": ["min_sample_size"],
        "profile": "vm_ul6_strict",
        "triage_status": status,
        "triage_reasons": ["min_sample_size"],
    }


class TestBuildRecord:
    def test_required_fields_stamped(self) -> None:
        row = build_record(
            run_id="r_abc",
            strategy_name="r47",
            instrument="TMFD6",
            strategy_type="maker",
            profile_name="vm_ul6_strict",
            advisory=_advisory(),
            blocking=_blocking(),
            recorded_at_ns=1_700_000_000_000_000_000,
        )
        assert row["schema_version"] == SCHEMA_VERSION
        assert row["run_id"] == "r_abc"
        assert row["strategy_type"] == "maker"
        assert row["triage_status"] == "sample_promising"
        assert row["triage_reasons"] == ["min_sample_size"]
        assert row["blocking_passed"] is False
        assert row["recorded_at_ns"] == 1_700_000_000_000_000_000

    def test_sub_gates_projected_to_stable_shape(self) -> None:
        row = build_record(
            run_id="r1",
            strategy_name="x",
            instrument="TXFD6",
            strategy_type="maker",
            profile_name="",
            advisory=_advisory(),
            blocking=None,
        )
        gates = {g["name"]: g for g in row["sub_gates"]}
        assert gates["min_sample_size"]["passed"] is False
        assert gates["min_sample_size"]["metrics"]["sample_adequacy_label"] == "promising"
        assert gates["edge_per_round_trip"]["passed"] is True

    def test_loose_profile_blocking_none_yields_empty_triage(self) -> None:
        row = build_record(
            run_id="r1",
            strategy_name="x",
            instrument="TXFD6",
            strategy_type="taker",
            profile_name="",
            advisory=_advisory(),
            blocking=None,
        )
        assert row["blocking_passed"] is None
        assert row["triage_status"] == ""
        assert row["triage_reasons"] == []

    def test_rejects_empty_run_id(self) -> None:
        with pytest.raises(ValueError, match="run_id"):
            build_record(
                run_id="",
                strategy_name="x",
                instrument="T",
                strategy_type="maker",
                profile_name="",
                advisory=None,
                blocking=None,
            )

    def test_rejects_unknown_strategy_type(self) -> None:
        with pytest.raises(ValueError, match="strategy_type"):
            build_record(
                run_id="r1",
                strategy_name="x",
                instrument="T",
                strategy_type="market_maker",  # type: ignore[arg-type]
                profile_name="",
                advisory=None,
                blocking=None,
            )


class TestRecordSubGateRun:
    def test_appends_one_line(self, _isolated_jsonl: Path) -> None:
        ok = record_sub_gate_run(
            run_id="r1",
            strategy_name="r47",
            instrument="TMFD6",
            strategy_type="maker",
            profile_name="vm_ul6_strict",
            advisory=_advisory(),
            blocking=_blocking(),
        )
        assert ok is True
        rows = _isolated_jsonl.read_text().splitlines()
        assert len(rows) == 1
        parsed = json.loads(rows[0])
        assert parsed["run_id"] == "r1"
        assert parsed["triage_status"] == "sample_promising"

    def test_duplicate_run_id_strategy_type_deduped(self, _isolated_jsonl: Path) -> None:
        kwargs = dict(
            run_id="r1",
            strategy_name="r47",
            instrument="TMFD6",
            strategy_type="maker",
            profile_name="vm_ul6_strict",
            advisory=_advisory(),
            blocking=_blocking(),
        )
        assert record_sub_gate_run(**kwargs) is True
        assert record_sub_gate_run(**kwargs) is False
        assert len(_isolated_jsonl.read_text().splitlines()) == 1

    def test_same_run_id_different_strategy_type_kept_separate(
        self, _isolated_jsonl: Path
    ) -> None:
        base = dict(
            run_id="r1",
            strategy_name="r47",
            instrument="TMFD6",
            profile_name="vm_ul6_strict",
            advisory=_advisory(),
            blocking=_blocking(),
        )
        assert record_sub_gate_run(strategy_type="maker", **base) is True
        assert record_sub_gate_run(strategy_type="taker", **base) is True
        assert len(_isolated_jsonl.read_text().splitlines()) == 2

    def test_read_runs_round_trip(self, _isolated_jsonl: Path) -> None:
        record_sub_gate_run(
            run_id="r1",
            strategy_name="r47",
            instrument="TMFD6",
            strategy_type="maker",
            profile_name="vm_ul6_strict",
            advisory=_advisory(),
            blocking=_blocking("killed"),
        )
        record_sub_gate_run(
            run_id="r2",
            strategy_name="r48",
            instrument="TXFD6",
            strategy_type="taker",
            profile_name="vm_ul6_strict",
            advisory=_advisory(),
            blocking=_blocking("passed"),
        )
        all_rows = read_runs()
        assert len(all_rows) == 2
        only_r1 = read_runs(run_id="r1")
        assert len(only_r1) == 1
        assert only_r1[0]["strategy_name"] == "r47"
        assert only_r1[0]["triage_status"] == "killed"

    def test_recovers_dedupe_cache_across_processes_via_warm(
        self, _isolated_jsonl: Path
    ) -> None:
        # Simulate a fresh process by clearing the cache after the first write;
        # the second write should still dedupe by reading the file.
        record_sub_gate_run(
            run_id="r1",
            strategy_name="r47",
            instrument="TMFD6",
            strategy_type="maker",
            profile_name="vm_ul6_strict",
            advisory=_advisory(),
            blocking=_blocking(),
        )
        sub_gate_audit._reset_cache_for_tests()
        ok = record_sub_gate_run(
            run_id="r1",
            strategy_name="r47",
            instrument="TMFD6",
            strategy_type="maker",
            profile_name="vm_ul6_strict",
            advisory=_advisory(),
            blocking=_blocking(),
        )
        assert ok is False
        assert len(_isolated_jsonl.read_text().splitlines()) == 1


# --- Round 17: spec provenance threading (goal §4) ------------------


class TestSpecProvenance:
    """The audit row should carry data_range / cost_model_id /
    required_gates from the candidate spec so future comparisons
    can attribute drift to spec changes rather than noise."""

    def test_schema_version_is_v2(self) -> None:
        from hft_platform.alpha.sub_gate_audit import SCHEMA_VERSION as ver
        assert ver == "sub_gate_run.v2"

    def test_row_omits_spec_provenance_when_not_provided(self) -> None:
        row = sub_gate_audit.build_record(
            run_id="r1",
            strategy_name="x",
            instrument="TXFD6",
            strategy_type="maker",
            profile_name="",
            advisory=_advisory(),
            blocking=None,
            spec_provenance=None,
        )
        assert "spec_provenance" not in row

    def test_row_includes_spec_provenance_when_provided(self) -> None:
        prov = {
            "data_range": "2026-01-02..2026-05-13",
            "cost_model_id": "shioaji_measured_p95+2bps",
            "required_gates": ["min_sample_size", "edge_per_round_trip"],
        }
        row = sub_gate_audit.build_record(
            run_id="r1",
            strategy_name="x",
            instrument="TXFD6",
            strategy_type="maker",
            profile_name="vm_ul6_strict",
            advisory=_advisory(),
            blocking=_blocking(),
            spec_provenance=prov,
        )
        assert row["spec_provenance"]["data_range"] == "2026-01-02..2026-05-13"
        assert row["spec_provenance"]["cost_model_id"] == "shioaji_measured_p95+2bps"
        assert row["spec_provenance"]["required_gates"] == [
            "min_sample_size",
            "edge_per_round_trip",
        ]

    def test_partial_provenance_fills_defaults(self) -> None:
        row = sub_gate_audit.build_record(
            run_id="r1",
            strategy_name="x",
            instrument="TXFD6",
            strategy_type="maker",
            profile_name="",
            advisory=_advisory(),
            blocking=None,
            spec_provenance={"data_range": "2026-Q1"},
        )
        prov = row["spec_provenance"]
        assert prov["data_range"] == "2026-Q1"
        assert prov["cost_model_id"] == ""
        assert prov["required_gates"] == []

    def test_non_list_required_gates_coerced_to_empty(self) -> None:
        row = sub_gate_audit.build_record(
            run_id="r1",
            strategy_name="x",
            instrument="TXFD6",
            strategy_type="maker",
            profile_name="",
            advisory=_advisory(),
            blocking=None,
            spec_provenance={"required_gates": "not_a_list"},
        )
        assert row["spec_provenance"]["required_gates"] == []

    def test_record_sub_gate_run_persists_provenance(
        self, _isolated_jsonl: Path
    ) -> None:
        sub_gate_audit.record_sub_gate_run(
            run_id="r1",
            strategy_name="r47",
            instrument="TMFD6",
            strategy_type="maker",
            profile_name="vm_ul6_strict",
            advisory=_advisory(),
            blocking=_blocking(),
            spec_provenance={
                "data_range": "2026-01-02..2026-05-13",
                "cost_model_id": "shioaji_measured_p95",
                "required_gates": ["min_sample_size"],
            },
        )
        rows = sub_gate_audit.read_runs(run_id="r1")
        assert len(rows) == 1
        prov = rows[0]["spec_provenance"]
        assert prov["data_range"] == "2026-01-02..2026-05-13"
        assert prov["cost_model_id"] == "shioaji_measured_p95"
