from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pytest
import yaml

import research.factory as factory
import research.tools.data_governance as data_governance


def _bootstrap_research_root(root: Path) -> None:
    (root / "alphas").mkdir(parents=True, exist_ok=True)
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "data" / "raw").mkdir(parents=True, exist_ok=True)
    (root / "data" / "interim").mkdir(parents=True, exist_ok=True)
    (root / "data" / "processed").mkdir(parents=True, exist_ok=True)


def _valid_strategy_spec() -> dict:
    return {
        "strategy_name": "c99_demo",
        "market": "TAIFEX",
        "instrument": "TXFD6",
        "hypothesis": "edge hypothesis",
        "timeframe": "5m",
        "holding_period": "intraday",
        "frequency_class": "intraday_hft",
        "entry_rule": "enter on governed signal",
        "exit_rule": "exit on stop or force-flat",
        "position_sizing": "fixed 1 lot",
        "risk_control": {
            "max_position": 1,
            "max_drawdown_pts": 80,
            "force_flat_rule": "13:25 TPE close",
        },
        "cost_model": {
            "fee_bps": 0.4,
            "tax_bps": 2.0,
            "slippage_pts": 0.5,
            "latency_profile": "shioaji_measured_p95",
        },
        "validation_plan": {
            "data_range": "2026-01-02..2026-05-13",
            "oos_split": "70/30 by trading day",
            "sample_targets": {
                "min_round_trips": 300,
                "min_oos_trading_days": 60,
            },
            "required_gates": ["min_sample_size", "edge_per_round_trip"],
            "net_edge_floor_pts": 10.0,
        },
    }


def _write_governed_alpha(root: Path, alpha_id: str, spec: dict) -> None:
    alpha_dir = root / "alphas" / alpha_id
    (alpha_dir / "tests").mkdir(parents=True, exist_ok=True)
    (alpha_dir / "__init__.py").write_text("", encoding="utf-8")
    (alpha_dir / "impl.py").write_text("ALPHA_ID = " + repr(alpha_id) + "\n", encoding="utf-8")
    (alpha_dir / "README.md").write_text(f"# {alpha_id}\n", encoding="utf-8")
    (alpha_dir / "tests" / "test_smoke.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    (alpha_dir / "spec.yaml").write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")


def _readiness_row(
    candidate: str,
    *,
    blockers: list[str] | None = None,
    full_edge: float | None = 18.0,
    oos_edge: float | None = 12.5,
    full_events: int | None = 360,
    oos_days: int | None = 70,
    drawdown_gate: bool | None = True,
    parity_status: str = "pass",
) -> dict:
    blockers = blockers or []
    return {
        "candidate": candidate,
        "readiness_status": "paper_live_candidate" if not blockers else "not_eligible",
        "paper_live_eligible": not blockers,
        "primary_blocker": blockers[0] if blockers else "",
        "blockers": blockers,
        "next_actions": ["paper_live_validation_ready"] if not blockers else ["inspect_candidate_blockers"],
        "command_families": [],
        "metrics": {
            "mean_net_edge_pts_per_trade": full_edge,
            "out_of_sample_mean_net_edge_pts_per_trade": oos_edge,
            "edge_floor_pts": 10.0,
            "full_events": full_events,
            "out_of_sample_trading_days": oos_days,
            "drawdown_within_2x_average_monthly_net_pnl": drawdown_gate,
            "out_of_sample_pnl_distribution_checked": True,
            "out_of_sample_loss_distribution_checked": True,
            "out_of_sample_single_trade_dominance_passed": True,
            "out_of_sample_single_day_dominance_passed": True,
            "parity_evidence_status": parity_status,
            "replay_match_pct": 96.0 if parity_status == "pass" else 80.0,
        },
        "summary_path": f"experiments/validations/{candidate}/20260605T000000Z_summary.json",
        "spec_path": f"alphas/{candidate}/spec.yaml",
    }


def _archive_advancement_payload() -> dict:
    return {
        "schema": "research.readiness_candidate_advancement.v1",
        "recommended_research_route": "archive_candidate_set",
        "recommended_candidate": "failed_a",
        "recommended_candidate_group": ["failed_a", "failed_b", "failed_c", "failed_d"],
        "candidates": [
            {
                "candidate": candidate,
                "advancement_status": "archive_candidate",
                "primary_reason": "multiple_core_conditions_failed_without_clear_repair_path",
                "supporting_metrics": {"mean_net_edge_pts_per_trade": 2.0},
                "blocking_factors": ["research_decision_failed", "drawdown_gate_failed"],
                "risk_flags": ["drawdown_risk", "failed_research_decision"],
                "summary_path": f"experiments/validations/{candidate}/summary.json",
                "spec_path": f"alphas/{candidate}/spec.yaml",
            }
            for candidate in ("failed_a", "failed_b", "failed_c", "failed_d")
        ]
        + [
            {
                "candidate": "t1f",
                "advancement_status": "sample_expansion_candidate",
                "primary_reason": "edge_signal_present_but_sample_or_oos_days_below_target",
                "supporting_metrics": {"mean_net_edge_pts_per_trade": 12.0},
                "blocking_factors": ["min_round_trips_not_met"],
                "risk_flags": [],
                "summary_path": "experiments/validations/t1f/summary.json",
                "spec_path": "alphas/t1f/spec.yaml",
            }
        ],
    }


def test_factory_candidate_advancement_classifies_statuses_and_route() -> None:
    readiness = {
        "schema": "research.readiness_summary.v1",
        "total_candidates": 7,
        "rows": [
            _readiness_row("ready_alpha"),
            _readiness_row("missing_evidence_alpha", blockers=["parity_evidence_missing"]),
            _readiness_row(
                "low_sample_alpha",
                blockers=["min_round_trips_not_met", "min_oos_trading_days_not_met"],
                full_events=120,
                oos_days=15,
            ),
            _readiness_row(
                "weak_edge_alpha",
                blockers=["full_edge_floor_not_cleared"],
                full_edge=8.5,
                oos_edge=11.0,
            ),
            _readiness_row(
                "parity_drift_alpha",
                blockers=["parity_evidence_fail"],
                parity_status="fail",
            ),
            _readiness_row(
                "artifact_gap_alpha",
                blockers=["validation_summary_missing", "research_decision_blocked_by_audit"],
                full_edge=None,
                oos_edge=None,
                full_events=None,
                oos_days=None,
                drawdown_gate=None,
                parity_status="missing",
            ),
            _readiness_row(
                "archive_alpha",
                blockers=[
                    "full_edge_floor_not_cleared",
                    "out_of_sample_edge_floor_not_cleared",
                    "min_round_trips_not_met",
                    "drawdown_gate_failed",
                    "research_decision_failed",
                ],
                full_edge=2.0,
                oos_edge=-1.0,
                full_events=30,
                drawdown_gate=False,
            ),
        ],
    }

    payload = factory._research_candidate_advancement_payload(readiness)

    assert payload["schema"] == "research.readiness_candidate_advancement.v1"
    assert payload["recommended_research_route"] == "prepare_paper_candidate"
    assert payload["recommended_candidate"] == "ready_alpha"
    assert payload["summary"]["counts_by_advancement_status"] == {
        "archive_candidate": 1,
        "artifact_repair_candidate": 1,
        "evidence_backfill_candidate": 1,
        "hypothesis_review_candidate": 1,
        "parity_repair_candidate": 1,
        "ready_for_paper": 1,
        "sample_expansion_candidate": 1,
    }
    assert [row["advancement_status"] for row in payload["candidates"]] == [
        "ready_for_paper",
        "evidence_backfill_candidate",
        "sample_expansion_candidate",
        "hypothesis_review_candidate",
        "parity_repair_candidate",
        "artifact_repair_candidate",
        "archive_candidate",
    ]
    for row in payload["candidates"]:
        assert row["primary_reason"]
        assert isinstance(row["supporting_metrics"], dict)
        assert isinstance(row["blocking_factors"], list)
        assert isinstance(row["evidence_gaps"], list)
        assert isinstance(row["risk_flags"], list)
        assert row["next_research_action"]
        assert row["owner_action_hint"]


def test_factory_candidate_advancement_route_uses_highest_priority_available_class() -> None:
    readiness = {
        "schema": "research.readiness_summary.v1",
        "total_candidates": 3,
        "rows": [
            _readiness_row("evidence_alpha", blockers=["out_of_sample_distribution_evidence_missing"]),
            _readiness_row("sample_alpha", blockers=["min_round_trips_not_met"], full_events=120),
            _readiness_row("parity_alpha", blockers=["parity_evidence_fail"], parity_status="fail"),
        ],
    }

    payload = factory._research_candidate_advancement_payload(readiness)

    assert payload["recommended_research_route"] == "backfill_evidence"
    assert payload["recommended_candidate"] == "evidence_alpha"


def test_factory_refinement_iteration_archives_target_group_and_advances_remaining_candidate() -> None:
    advancement = _archive_advancement_payload()

    archive, iteration = factory._research_refinement_iteration_payload(
        advancement,
        iteration_index=1,
        archive_output_path=Path("/tmp/archive.json"),
    )

    assert archive["schema"] == "research.candidate_archive_decision.v1"
    assert archive["decision"] == "archive_recommended"
    assert archive["destructive"] is False
    assert archive["candidate_group"] == ["failed_a", "failed_b", "failed_c", "failed_d"]
    assert [row["candidate"] for row in archive["candidates"]] == [
        "failed_a",
        "failed_b",
        "failed_c",
        "failed_d",
    ]
    assert archive["excluded_candidates"] == [{"candidate": "t1f", "advancement_status": "sample_expansion_candidate"}]
    assert all(row["recommended_status"] == "archive_recommended" for row in archive["candidates"])
    assert all(row["retained_artifacts"]["preserved"] is True for row in archive["candidates"])

    assert iteration["schema"] == "research.refinement_iteration.v1"
    assert iteration["iteration_index"] == 1
    assert iteration["status"] == "completed"
    assert iteration["selected_route"] == "archive_candidate_set"
    assert iteration["candidate_group"] == ["failed_a", "failed_b", "failed_c", "failed_d"]
    assert iteration["literature_refresh_triggered"] is False
    assert iteration["artifact_produced"] == str(Path("/tmp/archive.json").resolve())
    assert iteration["recommended_research_route"] == "expand_sample"
    assert iteration["next_action"] == "expand_sample_and_rerun_validation"
    assert iteration["errors"] == []


def test_factory_refinement_iteration_blocks_invalid_schema_and_iteration_index() -> None:
    advancement = _archive_advancement_payload()
    advancement["schema"] = "research.readiness_candidate_advancement.v0"

    archive, iteration = factory._research_refinement_iteration_payload(
        advancement,
        iteration_index=0,
        archive_output_path=Path("/tmp/archive.json"),
    )

    assert archive == {}
    assert iteration["status"] == "blocked"
    assert iteration["errors"] == ["invalid_advancement_schema", "invalid_iteration_index"]
    assert iteration["artifact_produced"] == ""


def test_factory_refinement_iteration_blocks_duplicate_and_missing_target_candidates() -> None:
    advancement = _archive_advancement_payload()
    advancement["candidates"].append(dict(advancement["candidates"][0]))
    advancement["recommended_candidate_group"].append("missing_candidate")

    archive, iteration = factory._research_refinement_iteration_payload(
        advancement,
        iteration_index=1,
        archive_output_path=Path("/tmp/archive.json"),
    )

    assert archive == {}
    assert iteration["status"] == "blocked"
    assert iteration["errors"] == [
        "duplicate_advancement_candidate",
        "target_candidate_missing",
    ]


def test_factory_refinement_iteration_blocks_duplicate_target_group_identity() -> None:
    advancement = _archive_advancement_payload()
    advancement["recommended_candidate_group"].append("failed_a")

    archive, iteration = factory._research_refinement_iteration_payload(
        advancement,
        iteration_index=1,
        archive_output_path=Path("/tmp/archive.json"),
    )

    assert archive == {}
    assert iteration["status"] == "blocked"
    assert iteration["errors"] == ["duplicate_target_candidate"]


def test_factory_refinement_iteration_blocks_empty_group_and_route_status_mismatch() -> None:
    advancement = _archive_advancement_payload()
    advancement["recommended_candidate_group"] = []

    _, empty_group_iteration = factory._research_refinement_iteration_payload(
        advancement,
        iteration_index=1,
        archive_output_path=Path("/tmp/archive.json"),
    )

    assert empty_group_iteration["status"] == "blocked"
    assert empty_group_iteration["errors"] == ["empty_recommended_candidate_group"]

    advancement = _archive_advancement_payload()
    advancement["candidates"][0]["advancement_status"] = "hypothesis_review_candidate"

    _, mismatch_iteration = factory._research_refinement_iteration_payload(
        advancement,
        iteration_index=1,
        archive_output_path=Path("/tmp/archive.json"),
    )

    assert mismatch_iteration["status"] == "blocked"
    assert mismatch_iteration["errors"] == ["target_status_route_mismatch"]


def test_factory_refinement_iteration_blocks_routes_not_implemented_in_this_slice() -> None:
    advancement = _archive_advancement_payload()
    advancement["recommended_research_route"] = "expand_sample"
    advancement["recommended_candidate"] = "t1f"
    advancement["recommended_candidate_group"] = ["t1f"]

    archive, iteration = factory._research_refinement_iteration_payload(
        advancement,
        iteration_index=1,
        archive_output_path=Path("/tmp/archive.json"),
    )

    assert archive == {}
    assert iteration["status"] == "blocked"
    assert iteration["errors"] == ["route_not_implemented_in_this_slice"]


def test_factory_parser_exposes_refinement_iteration() -> None:
    parser = factory.build_parser()
    args = parser.parse_args(
        [
            "refinement-iteration",
            "--iteration-index",
            "2",
            "--archive-out",
            "archive.json",
            "--out",
            "iteration.json",
        ]
    )

    assert args.func is factory.cmd_refinement_iteration
    assert args.iteration_index == 2
    assert args.archive_out == "archive.json"
    assert args.out == "iteration.json"


def test_factory_parser_rejects_non_positive_refinement_iteration_index() -> None:
    parser = factory.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["refinement-iteration", "--iteration-index", "0"])


def test_factory_refinement_iteration_command_writes_archive_and_iteration_artifacts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    advancement = _archive_advancement_payload()

    monkeypatch.setattr(factory, "_build_research_candidate_advancement", lambda: advancement)
    archive_out = tmp_path / "archive.json"
    iteration_out = tmp_path / "iteration.json"

    rc = factory.cmd_refinement_iteration(
        argparse.Namespace(
            iteration_index=1,
            archive_out=str(archive_out),
            out=str(iteration_out),
        )
    )

    assert rc == 0
    archive = json.loads(archive_out.read_text(encoding="utf-8"))
    iteration = json.loads(iteration_out.read_text(encoding="utf-8"))
    assert archive["candidate_group"] == ["failed_a", "failed_b", "failed_c", "failed_d"]
    assert archive["excluded_candidates"] == [{"candidate": "t1f", "advancement_status": "sample_expansion_candidate"}]
    assert iteration["status"] == "completed"
    assert iteration["recommended_research_route"] == "expand_sample"
    assert iteration["artifact_produced"] == str(archive_out.resolve())


def test_factory_refinement_iteration_command_writes_blocked_iteration_without_archive(
    monkeypatch,
    tmp_path: Path,
) -> None:
    advancement = _archive_advancement_payload()
    advancement["recommended_research_route"] = "expand_sample"
    advancement["recommended_candidate"] = "t1f"
    advancement["recommended_candidate_group"] = ["t1f"]

    monkeypatch.setattr(factory, "_build_research_candidate_advancement", lambda: advancement)
    archive_out = tmp_path / "archive.json"
    iteration_out = tmp_path / "iteration.json"

    rc = factory.cmd_refinement_iteration(
        argparse.Namespace(
            iteration_index=1,
            archive_out=str(archive_out),
            out=str(iteration_out),
        )
    )

    assert rc == 1
    assert not archive_out.exists()
    iteration = json.loads(iteration_out.read_text(encoding="utf-8"))
    assert iteration["status"] == "blocked"
    assert iteration["errors"] == ["route_not_implemented_in_this_slice"]


def test_audit_scoped_data_paths_ignore_unrelated_datasets(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)

    unrelated = root / "data" / "raw" / "unrelated.npy"
    np.save(unrelated, np.zeros(8, dtype=np.float64))

    scoped = root / "data" / "interim" / "scoped.npy"
    arr = np.zeros(8, dtype=[("price", "f8"), ("qty", "f8")])
    np.save(scoped, arr)
    rc = data_governance.cmd_stamp_data_meta(
        argparse.Namespace(
            data=str(scoped),
            dataset_id="scoped_v1",
            source_type="synthetic",
            source="unit_test",
            owner="tests",
            schema_version=1,
            symbols="TXF",
            split="full",
            out=None,
        )
    )
    assert rc == 0

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "audit.json"
    rc = factory.cmd_audit(
        argparse.Namespace(
            out=str(out),
            fail_on_warning=False,
            data=[str(scoped)],
        )
    )
    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    gov = payload["details"]["data_governance"]
    assert gov["scope"] == "scoped_data_paths"
    assert gov["missing_metadata_sidecars"] == []
    assert gov["invalid_metadata_sidecars"] == {}
    assert gov["scanned_datasets"] == ["data/interim/scoped.npy"]


def test_audit_scoped_data_paths_reject_invalid_metadata(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)

    scoped = root / "data" / "interim" / "scoped_bad.npy"
    np.save(scoped, np.zeros((6, 2), dtype=np.float64))
    meta = scoped.with_suffix(scoped.suffix + ".meta.json")
    meta.write_text(
        json.dumps(
            {
                "dataset_id": "scoped_bad",
                "source_type": "real",
                "owner": "tests",
                "schema_version": 1,
                "rows": 6,
                "fields": [],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "audit_bad.json"
    rc = factory.cmd_audit(
        argparse.Namespace(
            out=str(out),
            fail_on_warning=False,
            data=[str(scoped)],
        )
    )
    assert rc == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    gov = payload["details"]["data_governance"]
    assert "data/interim/scoped_bad.npy" in gov["invalid_metadata_sidecars"]
    assert "fields_must_be_nonempty_list" in gov["invalid_metadata_sidecars"]["data/interim/scoped_bad.npy"]
    assert any("metadata sidecar invalid" in err for err in payload["errors"])


def test_factory_audit_reports_fixed_strategy_spec_template_and_candidate_gaps(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    template = _valid_strategy_spec()
    template.pop("cost_model")
    (root / "templates").mkdir(parents=True)
    (root / "templates" / "strategy_spec.yaml").write_text(
        yaml.safe_dump(template, sort_keys=False),
        encoding="utf-8",
    )
    good_template = _valid_strategy_spec()
    (root / "alphas" / "_templates").mkdir(parents=True)
    (root / "alphas" / "_templates" / "spec.yaml").write_text(
        yaml.safe_dump(good_template, sort_keys=False),
        encoding="utf-8",
    )

    _write_governed_alpha(root, "good_alpha", _valid_strategy_spec())
    bad_spec = _valid_strategy_spec()
    bad_spec.pop("entry_rule")
    bad_spec["validation_plan"]["net_edge_floor_pts"] = 5.0
    _write_governed_alpha(root, "bad_alpha", bad_spec)

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "audit_strategy_specs.json"
    rc = factory.cmd_audit(argparse.Namespace(out=str(out), fail_on_warning=False, data=[]))

    assert rc == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    spec_audit = payload["details"]["strategy_spec_fixed_template_audit"]
    assert "cost_model" in spec_audit["required_top_level_fields"]
    assert spec_audit["template_missing_required"] == [
        {
            "path": "templates/strategy_spec.yaml",
            "missing": ["cost_model"],
        }
    ]
    assert spec_audit["candidate_invalid"] == [
        {
            "path": "alphas/bad_alpha/spec.yaml",
            "errors": [
                "missing or empty required field: 'entry_rule'",
                "validation_plan.net_edge_floor_pts < 10.0 — goal 限制 §3 forbids relaxing the > 10 pts/trade bar",
            ],
        }
    ]
    assert "alphas/good_alpha/spec.yaml" in spec_audit["candidate_valid"]
    assert any("strategy spec template" in error for error in payload["errors"])
    assert any("candidate strategy spec invalid" in error for error in payload["errors"])


def test_factory_audit_generates_research_record_from_spec_and_validation_summary(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    spec = _valid_strategy_spec()
    spec["strategy_name"] = "record_alpha"
    _write_governed_alpha(root, "record_alpha", spec)

    validation_dir = root / "experiments" / "validations" / "record_alpha"
    validation_dir.mkdir(parents=True)
    summary_path = validation_dir / "20260603T000000Z_summary.json"
    summary = {
        "candidate": "record_alpha",
        "artifact_scope": "validation_summary",
        "summary_path": str(summary_path),
        "edge_floor_metric": "mean_net_edge_pts_per_trade",
        "edge_floor_cleared": False,
        "research_decision": {
            "status": "failed",
            "reason": "edge_floor_not_cleared",
            "evidence": ["mean_net_edge_pts_per_trade"],
            "decided_by": "unit_test_gate",
        },
        "hard_gate": {"drawdown_within_2x_average_monthly_net_pnl": False},
        "splits": {
            "full": {
                "events": 120,
                "trading_days": 45,
                "mean_net_edge_pts_per_trade": 8.5,
                "max_drawdown_net_pts": 42.0,
                "average_monthly_net_pnl": 12.0,
                "median_monthly_net_pnl": 11.0,
                "worst_month_net_pnl": -5.0,
            },
            "out_of_sample": {
                "events": 40,
                "trading_days": 15,
                "mean_net_edge_pts_per_trade": 7.0,
                "max_drawdown_net_pts": 18.0,
                "average_monthly_net_pnl": 9.0,
                "median_monthly_net_pnl": 9.0,
                "worst_month_net_pnl": -2.0,
            },
        },
        "definition": {
            "months": ["B6"],
            "max_date": "2026-04-15",
            "cost_pts": 8.0,
        },
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "audit_research_records.json"
    rc = factory.cmd_audit(argparse.Namespace(out=str(out), fail_on_warning=False, data=[]))

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    record_audit = payload["details"]["research_record_generation"]
    assert record_audit["incomplete_records"] == []
    assert record_audit["complete_records"] == [
        {
            "candidate": "record_alpha",
            "spec_path": "alphas/record_alpha/spec.yaml",
            "summary_path": "experiments/validations/record_alpha/20260603T000000Z_summary.json",
            "strategy_name": "record_alpha",
            "market": "TAIFEX",
            "instrument": "TXFD6",
            "hypothesis": "edge hypothesis",
            "timeframe": "5m",
            "holding_period": "intraday",
            "entry_rule": "enter on governed signal",
            "exit_rule": "exit on stop or force-flat",
            "position_sizing": "fixed 1 lot",
            "risk_control": spec["risk_control"],
            "cost_assumptions": spec["cost_model"],
            "validation_plan": spec["validation_plan"],
            "data_range": "2026-01-02..2026-05-13",
            "parameters": summary["definition"],
            "full_results": {
                "events": 120,
                "trading_days": 45,
                "mean_net_edge_pts_per_trade": 8.5,
            },
            "out_of_sample_results": {
                "events": 40,
                "trading_days": 15,
                "mean_net_edge_pts_per_trade": 7.0,
            },
            "risk_metrics": {
                "full_max_drawdown_net_pts": 42.0,
                "full_average_monthly_net_pnl": 12.0,
                "full_median_monthly_net_pnl": 11.0,
                "full_worst_month_net_pnl": -5.0,
                "out_of_sample_max_drawdown_net_pts": 18.0,
                "out_of_sample_average_monthly_net_pnl": 9.0,
                "out_of_sample_median_monthly_net_pnl": 9.0,
                "out_of_sample_worst_month_net_pnl": -2.0,
                "drawdown_within_2x_average_monthly_net_pnl": False,
            },
            "research_decision": summary["research_decision"],
        }
    ]


def test_factory_audit_marks_scaffolded_alpha_without_evidence_blocked_by_audit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    spec = _valid_strategy_spec()
    spec["strategy_name"] = "scaffold_only_alpha"
    _write_governed_alpha(root, "scaffold_only_alpha", spec)

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "audit_scaffold_only.json"
    rc = factory.cmd_audit(argparse.Namespace(out=str(out), fail_on_warning=False, data=[]))

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    record_audit = payload["details"]["research_record_generation"]
    assert record_audit["complete_records"] == []
    assert record_audit["incomplete_records"] == [
        {
            "candidate": "scaffold_only_alpha",
            "strategy_name": "scaffold_only_alpha",
            "spec_path": "alphas/scaffold_only_alpha/spec.yaml",
            "summary_path": "",
            "missing": ["validation_summary"],
        }
    ]
    comparison = payload["details"]["research_candidate_comparison"]
    assert comparison["paper_live_candidates"] == []
    assert comparison["not_eligible"] == ["scaffold_only_alpha"]
    assert comparison["rows"] == [
        {
            "candidate": "scaffold_only_alpha",
            "strategy_name": "scaffold_only_alpha",
            "market": "TAIFEX",
            "instrument": "TXFD6",
            "timeframe": "5m",
            "holding_period": "intraday",
            "data_range": "2026-01-02..2026-05-13",
            "spec_path": "alphas/scaffold_only_alpha/spec.yaml",
            "summary_path": "",
            "mean_net_edge_pts_per_trade": None,
            "out_of_sample_mean_net_edge_pts_per_trade": None,
            "edge_floor_pts": 10.0,
            "full_events": None,
            "out_of_sample_trading_days": None,
            "min_round_trips": 300,
            "min_oos_trading_days": 60,
            "drawdown_within_2x_average_monthly_net_pnl": None,
            "replay_match_pct": None,
            "parity_evidence_status": "missing",
            "research_decision_status": "blocked_by_audit",
            "research_decision_reason": "missing_validation_summary",
            "paper_live_eligible": False,
            "eligibility_status": "blocked_by_audit",
            "blockers": [
                "validation_summary_missing",
                "research_decision_blocked_by_audit",
                "parity_evidence_missing",
            ],
        }
    ]
    readiness = payload["details"]["research_readiness_summary"]
    assert readiness["counts_by_status"] == {"blocked_by_audit": 1}
    assert readiness["counts_by_blocker"] == {
        "parity_evidence_missing": 1,
        "research_decision_blocked_by_audit": 1,
        "validation_summary_missing": 1,
    }
    assert readiness["rows"][0]["next_actions"] == [
        "run_validation_summary_generation_before_readiness",
        "provide_or_attach_replay_paper_live_parity_evidence",
    ]


def test_factory_audit_compares_research_candidates_with_uniform_metrics_and_blockers(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")

    def write_candidate(
        alpha_id: str,
        *,
        full_edge: float,
        oos_edge: float,
        full_events: int,
        oos_days: int,
        drawdown_gate: bool,
        decision_status: str,
        decision_reason: str,
        promotion_blockers: list[str] | None = None,
        parity_evidence: dict | None = None,
    ) -> None:
        spec = _valid_strategy_spec()
        spec["strategy_name"] = alpha_id
        spec["validation_plan"]["promotion_blockers"] = promotion_blockers or []
        _write_governed_alpha(root, alpha_id, spec)
        validation_dir = root / "experiments" / "validations" / alpha_id
        validation_dir.mkdir(parents=True)
        summary_path = validation_dir / "20260603T000000Z_summary.json"
        summary = {
            "candidate": alpha_id,
            "artifact_scope": "validation_summary",
            "summary_path": str(summary_path),
            "edge_floor_metric": "mean_net_edge_pts_per_trade",
            "edge_floor_cleared": full_edge > 10.0,
            "research_decision": {
                "status": decision_status,
                "reason": decision_reason,
                "evidence": ["mean_net_edge_pts_per_trade"],
                "decided_by": "unit_test_gate",
            },
            "hard_gate": {"drawdown_within_2x_average_monthly_net_pnl": drawdown_gate},
            "splits": {
                "full": {
                    "events": full_events,
                    "trading_days": 90,
                    "mean_net_edge_pts_per_trade": full_edge,
                    "max_drawdown_net_pts": 20.0,
                    "average_monthly_net_pnl": 20.0,
                    "median_monthly_net_pnl": 19.0,
                    "worst_month_net_pnl": 5.0,
                    "drawdown_within_2x_average_monthly_net_pnl": drawdown_gate,
                },
                "out_of_sample": {
                    "events": 120,
                    "trading_days": oos_days,
                    "mean_net_edge_pts_per_trade": oos_edge,
                    "max_drawdown_net_pts": 12.0,
                    "average_monthly_net_pnl": 18.0,
                    "median_monthly_net_pnl": 17.0,
                    "worst_month_net_pnl": 4.0,
                    "drawdown_within_2x_average_monthly_net_pnl": drawdown_gate,
                    "pnl_distribution_checked": True,
                    "loss_distribution_checked": True,
                    "single_trade_dominance_passed": True,
                    "single_day_dominance_passed": True,
                },
            },
            "definition": {"cost_pts": 8.0},
        }
        if parity_evidence is not None:
            summary["parity_evidence"] = parity_evidence
        summary_path.write_text(json.dumps(summary), encoding="utf-8")

    write_candidate(
        "candidate_keep",
        full_edge=18.0,
        oos_edge=12.5,
        full_events=360,
        oos_days=70,
        drawdown_gate=True,
        decision_status="promising",
        decision_reason="unit_test_candidate",
        parity_evidence={
            "artifact_scope": "parity_evidence",
            "match_pct": 96.0,
            "threshold": 95.0,
            "checked_dimensions": [
                "signal_trigger_time",
                "direction",
                "position_size",
                "entry",
                "exit",
                "session_filter",
                "risk_filter",
                "force_flat_rule",
            ],
            "mismatch_counts": {"latency_shift": 1},
        },
    )
    write_candidate(
        "candidate_kill",
        full_edge=8.0,
        oos_edge=6.5,
        full_events=140,
        oos_days=20,
        drawdown_gate=False,
        decision_status="failed",
        decision_reason="unit_test_kill",
        promotion_blockers=["no_replay_paper_live_parity_evidence_yet"],
    )

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "audit_candidate_comparison.json"
    rc = factory.cmd_audit(argparse.Namespace(out=str(out), fail_on_warning=False, data=[]))

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    comparison = payload["details"]["research_candidate_comparison"]
    assert comparison["paper_live_candidates"] == ["candidate_keep"]
    assert comparison["not_eligible"] == ["candidate_kill"]
    assert comparison["rows"] == [
        {
            "candidate": "candidate_keep",
            "strategy_name": "candidate_keep",
            "market": "TAIFEX",
            "instrument": "TXFD6",
            "timeframe": "5m",
            "holding_period": "intraday",
            "data_range": "2026-01-02..2026-05-13",
            "spec_path": "alphas/candidate_keep/spec.yaml",
            "summary_path": "experiments/validations/candidate_keep/20260603T000000Z_summary.json",
            "mean_net_edge_pts_per_trade": 18.0,
            "out_of_sample_mean_net_edge_pts_per_trade": 12.5,
            "edge_floor_pts": 10.0,
            "full_events": 360,
            "out_of_sample_trading_days": 70,
            "min_round_trips": 300,
            "min_oos_trading_days": 60,
            "drawdown_within_2x_average_monthly_net_pnl": True,
            "out_of_sample_pnl_distribution_checked": True,
            "out_of_sample_loss_distribution_checked": True,
            "out_of_sample_single_trade_dominance_passed": True,
            "out_of_sample_single_day_dominance_passed": True,
            "replay_match_pct": 96.0,
            "parity_evidence_status": "pass",
            "research_decision_status": "promising",
            "research_decision_reason": "unit_test_candidate",
            "paper_live_eligible": True,
            "eligibility_status": "paper_live_candidate",
            "blockers": [],
        },
        {
            "candidate": "candidate_kill",
            "strategy_name": "candidate_kill",
            "market": "TAIFEX",
            "instrument": "TXFD6",
            "timeframe": "5m",
            "holding_period": "intraday",
            "data_range": "2026-01-02..2026-05-13",
            "spec_path": "alphas/candidate_kill/spec.yaml",
            "summary_path": "experiments/validations/candidate_kill/20260603T000000Z_summary.json",
            "mean_net_edge_pts_per_trade": 8.0,
            "out_of_sample_mean_net_edge_pts_per_trade": 6.5,
            "edge_floor_pts": 10.0,
            "full_events": 140,
            "out_of_sample_trading_days": 20,
            "min_round_trips": 300,
            "min_oos_trading_days": 60,
            "drawdown_within_2x_average_monthly_net_pnl": False,
            "out_of_sample_pnl_distribution_checked": True,
            "out_of_sample_loss_distribution_checked": True,
            "out_of_sample_single_trade_dominance_passed": True,
            "out_of_sample_single_day_dominance_passed": True,
            "replay_match_pct": None,
            "parity_evidence_status": "missing",
            "research_decision_status": "failed",
            "research_decision_reason": "unit_test_kill",
            "paper_live_eligible": False,
            "eligibility_status": "failed",
            "blockers": [
                "full_edge_floor_not_cleared",
                "out_of_sample_edge_floor_not_cleared",
                "min_round_trips_not_met",
                "min_oos_trading_days_not_met",
                "drawdown_gate_failed",
                "research_decision_failed",
                "validation_plan_promotion_blockers",
                "parity_evidence_missing",
            ],
        },
    ]
    readiness = payload["details"]["research_readiness_summary"]
    assert readiness["schema"] == "research.readiness_summary.v1"
    assert readiness["total_candidates"] == 2
    assert readiness["paper_live_candidates"] == ["candidate_keep"]
    assert readiness["counts_by_status"] == {"paper_live_candidate": 1, "failed": 1}
    assert readiness["counts_by_blocker"] == {
        "drawdown_gate_failed": 1,
        "full_edge_floor_not_cleared": 1,
        "min_oos_trading_days_not_met": 1,
        "min_round_trips_not_met": 1,
        "out_of_sample_edge_floor_not_cleared": 1,
        "parity_evidence_missing": 1,
        "research_decision_failed": 1,
        "validation_plan_promotion_blockers": 1,
    }
    assert readiness["rows"] == [
        {
            "candidate": "candidate_keep",
            "readiness_status": "paper_live_candidate",
            "paper_live_eligible": True,
            "primary_blocker": "",
            "blockers": [],
            "command_families": [],
            "next_actions": ["paper_live_validation_ready"],
            "metrics": {
                "mean_net_edge_pts_per_trade": 18.0,
                "out_of_sample_mean_net_edge_pts_per_trade": 12.5,
                "edge_floor_pts": 10.0,
                "full_events": 360,
                "out_of_sample_trading_days": 70,
                "drawdown_within_2x_average_monthly_net_pnl": True,
                "out_of_sample_pnl_distribution_checked": True,
                "out_of_sample_loss_distribution_checked": True,
                "out_of_sample_single_trade_dominance_passed": True,
                "out_of_sample_single_day_dominance_passed": True,
                "parity_evidence_status": "pass",
                "replay_match_pct": 96.0,
            },
            "summary_path": "experiments/validations/candidate_keep/20260603T000000Z_summary.json",
            "spec_path": "alphas/candidate_keep/spec.yaml",
        },
        {
            "candidate": "candidate_kill",
            "readiness_status": "failed",
            "paper_live_eligible": False,
            "primary_blocker": "full_edge_floor_not_cleared",
            "blockers": [
                "full_edge_floor_not_cleared",
                "out_of_sample_edge_floor_not_cleared",
                "min_round_trips_not_met",
                "min_oos_trading_days_not_met",
                "drawdown_gate_failed",
                "research_decision_failed",
                "validation_plan_promotion_blockers",
                "parity_evidence_missing",
            ],
            "command_families": [
                {
                    "blocker": "parity_evidence_missing",
                    "command_family": "parity_evidence",
                    "attach_target": "validation_summary.parity_evidence",
                    "commands": [
                        "parity-evidence-backfill-plan",
                        "parity-evidence-template",
                        "parity-evidence-validate",
                        "parity-evidence-attach",
                    ],
                },
            ],
            "next_actions": [
                "retain_failed_research_record",
                "stop_or_form_new_hypothesis_after_edge_failure",
                "collect_more_sample_before_completion",
                "review_drawdown_monthly_distribution",
                "clear_validation_plan_promotion_blockers",
                "provide_or_attach_replay_paper_live_parity_evidence",
            ],
            "metrics": {
                "mean_net_edge_pts_per_trade": 8.0,
                "out_of_sample_mean_net_edge_pts_per_trade": 6.5,
                "edge_floor_pts": 10.0,
                "full_events": 140,
                "out_of_sample_trading_days": 20,
                "drawdown_within_2x_average_monthly_net_pnl": False,
                "out_of_sample_pnl_distribution_checked": True,
                "out_of_sample_loss_distribution_checked": True,
                "out_of_sample_single_trade_dominance_passed": True,
                "out_of_sample_single_day_dominance_passed": True,
                "parity_evidence_status": "missing",
                "replay_match_pct": None,
            },
            "summary_path": "experiments/validations/candidate_kill/20260603T000000Z_summary.json",
            "spec_path": "alphas/candidate_kill/spec.yaml",
        },
    ]


def test_factory_readiness_blocks_oos_distribution_dominance_failures(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    spec = _valid_strategy_spec()
    spec["strategy_name"] = "oos_dominated_candidate"
    _write_governed_alpha(root, "oos_dominated_candidate", spec)
    validation_dir = root / "experiments" / "validations" / "oos_dominated_candidate"
    validation_dir.mkdir(parents=True)
    summary_path = validation_dir / "20260604T000000Z_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "candidate": "oos_dominated_candidate",
                "artifact_scope": "validation_summary",
                "summary_path": str(summary_path),
                "edge_floor_metric": "mean_net_edge_pts_per_trade",
                "edge_floor_cleared": True,
                "research_decision": {
                    "status": "promising",
                    "reason": "edge_cleared_before_distribution_audit",
                    "evidence": ["mean_net_edge_pts_per_trade"],
                    "decided_by": "unit_test_gate",
                },
                "hard_gate": {"drawdown_within_2x_average_monthly_net_pnl": True},
                "splits": {
                    "full": {
                        "events": 360,
                        "trading_days": 90,
                        "mean_net_edge_pts_per_trade": 18.0,
                    },
                    "out_of_sample": {
                        "events": 120,
                        "trading_days": 70,
                        "mean_net_edge_pts_per_trade": 12.5,
                        "pnl_distribution_checked": True,
                        "loss_distribution_checked": False,
                        "single_trade_dominance_passed": True,
                        "single_day_dominance_passed": False,
                    },
                },
                "parity_evidence": {
                    "artifact_scope": "parity_evidence",
                    "match_pct": 96.0,
                    "threshold": 95.0,
                    "checked_dimensions": [
                        "signal_trigger_time",
                        "direction",
                        "position_size",
                        "entry",
                        "exit",
                        "session_filter",
                        "risk_filter",
                        "force_flat_rule",
                    ],
                    "mismatch_counts": {"latency_shift": 1},
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "readiness_oos_dominance.json"
    rc = factory.cmd_readiness_summary(argparse.Namespace(out=str(out)))

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["paper_live_candidates"] == []
    assert payload["counts_by_status"] == {"blocked_by_audit": 1}
    assert payload["counts_by_blocker"] == {
        "out_of_sample_loss_distribution_not_checked": 1,
        "out_of_sample_single_day_dominance_failed": 1,
    }
    row = payload["rows"][0]
    assert row["readiness_status"] == "blocked_by_audit"
    assert row["paper_live_eligible"] is False
    assert row["blockers"] == [
        "out_of_sample_loss_distribution_not_checked",
        "out_of_sample_single_day_dominance_failed",
    ]
    assert row["next_actions"] == ["review_out_of_sample_distribution_dominance"]
    assert row["metrics"] == {
        "mean_net_edge_pts_per_trade": 18.0,
        "out_of_sample_mean_net_edge_pts_per_trade": 12.5,
        "edge_floor_pts": 10.0,
        "full_events": 360,
        "out_of_sample_trading_days": 70,
        "drawdown_within_2x_average_monthly_net_pnl": True,
        "out_of_sample_pnl_distribution_checked": True,
        "out_of_sample_loss_distribution_checked": False,
        "out_of_sample_single_trade_dominance_passed": True,
        "out_of_sample_single_day_dominance_passed": False,
        "parity_evidence_status": "pass",
        "replay_match_pct": 96.0,
    }


def test_factory_readiness_summary_command_writes_operator_report(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    spec = _valid_strategy_spec()
    spec["strategy_name"] = "blocked_by_parity_alpha"
    _write_governed_alpha(root, "blocked_by_parity_alpha", spec)
    validation_dir = root / "experiments" / "validations" / "blocked_by_parity_alpha"
    validation_dir.mkdir(parents=True)
    summary_path = validation_dir / "20260604T000000Z_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "candidate": "blocked_by_parity_alpha",
                "artifact_scope": "validation_summary",
                "summary_path": str(summary_path),
                "edge_floor_metric": "mean_net_edge_pts_per_trade",
                "edge_floor_cleared": True,
                "research_decision": {
                    "status": "promising",
                    "reason": "unit_test_parity_gap",
                    "evidence": ["mean_net_edge_pts_per_trade"],
                    "decided_by": "unit_test_gate",
                },
                "hard_gate": {"drawdown_within_2x_average_monthly_net_pnl": True},
                "splits": {
                    "full": {
                        "events": 360,
                        "trading_days": 90,
                        "mean_net_edge_pts_per_trade": 18.0,
                    },
                    "out_of_sample": {
                        "events": 120,
                        "trading_days": 70,
                        "mean_net_edge_pts_per_trade": 12.5,
                        "pnl_distribution_checked": True,
                        "loss_distribution_checked": True,
                        "single_trade_dominance_passed": True,
                        "single_day_dominance_passed": True,
                    },
                },
                "definition": {"cost_pts": 8.0},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "readiness_summary.json"
    rc = factory.cmd_readiness_summary(argparse.Namespace(out=str(out)))

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == "research.readiness_summary.v1"
    assert payload["paper_live_candidates"] == []
    assert payload["counts_by_status"] == {"blocked_by_parity": 1}
    assert payload["rows"][0]["candidate"] == "blocked_by_parity_alpha"
    assert payload["rows"][0]["readiness_status"] == "blocked_by_parity"
    assert payload["rows"][0]["next_actions"] == ["provide_or_attach_replay_paper_live_parity_evidence"]


def test_factory_readiness_summary_lists_blocker_command_families(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    spec = _valid_strategy_spec()
    spec["strategy_name"] = "missing_evidence_commands_alpha"
    _write_governed_alpha(root, "missing_evidence_commands_alpha", spec)
    validation_dir = root / "experiments" / "validations" / "missing_evidence_commands_alpha"
    validation_dir.mkdir(parents=True)
    summary_path = validation_dir / "20260604T000000Z_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "candidate": "missing_evidence_commands_alpha",
                "artifact_scope": "validation_summary",
                "summary_path": str(summary_path),
                "edge_floor_metric": "mean_net_edge_pts_per_trade",
                "edge_floor_cleared": True,
                "research_decision": {
                    "status": "promising",
                    "reason": "unit_test_missing_evidence",
                    "evidence": ["mean_net_edge_pts_per_trade"],
                    "decided_by": "unit_test_gate",
                },
                "hard_gate": {"drawdown_within_2x_average_monthly_net_pnl": True},
                "splits": {
                    "full": {
                        "events": 360,
                        "trading_days": 90,
                        "mean_net_edge_pts_per_trade": 18.0,
                    },
                    "out_of_sample": {
                        "events": 120,
                        "trading_days": 70,
                        "mean_net_edge_pts_per_trade": 12.5,
                    },
                },
                "definition": {"cost_pts": 8.0},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "readiness_commands.json"
    rc = factory.cmd_readiness_summary(argparse.Namespace(out=str(out)))

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["paper_live_candidates"] == []
    assert payload["counts_by_blocker"] == {
        "out_of_sample_distribution_evidence_missing": 1,
        "parity_evidence_missing": 1,
    }
    assert payload["command_families_by_blocker"] == {
        "out_of_sample_distribution_evidence_missing": {
            "command_family": "oos_distribution_evidence",
            "attach_target": "validation_summary.splits.out_of_sample",
            "commands": [
                "oos-distribution-evidence-backfill-plan",
                "oos-distribution-evidence-template",
                "oos-distribution-evidence-validate",
                "oos-distribution-evidence-attach",
            ],
        },
        "parity_evidence_missing": {
            "command_family": "parity_evidence",
            "attach_target": "validation_summary.parity_evidence",
            "commands": [
                "parity-evidence-backfill-plan",
                "parity-evidence-template",
                "parity-evidence-validate",
                "parity-evidence-attach",
            ],
        },
    }
    assert payload["rows"][0]["next_actions"] == [
        "review_out_of_sample_distribution_dominance",
        "provide_or_attach_replay_paper_live_parity_evidence",
    ]
    assert payload["rows"][0]["command_families"] == [
        {
            "blocker": "out_of_sample_distribution_evidence_missing",
            "command_family": "oos_distribution_evidence",
            "attach_target": "validation_summary.splits.out_of_sample",
            "commands": [
                "oos-distribution-evidence-backfill-plan",
                "oos-distribution-evidence-template",
                "oos-distribution-evidence-validate",
                "oos-distribution-evidence-attach",
            ],
        },
        {
            "blocker": "parity_evidence_missing",
            "command_family": "parity_evidence",
            "attach_target": "validation_summary.parity_evidence",
            "commands": [
                "parity-evidence-backfill-plan",
                "parity-evidence-template",
                "parity-evidence-validate",
                "parity-evidence-attach",
            ],
        },
    ]


def test_factory_readiness_backfill_queue_only_queues_evidence_backfill_candidates() -> None:
    parity_family = {
        "blocker": "parity_evidence_missing",
        "command_family": "parity_evidence",
        "attach_target": "validation_summary.parity_evidence",
        "commands": [
            "parity-evidence-backfill-plan",
            "parity-evidence-template",
            "parity-evidence-validate",
            "parity-evidence-attach",
        ],
    }
    readiness_rows = [
        _readiness_row("evidence_candidate", blockers=["parity_evidence_missing"]),
        _readiness_row(
            "archive_candidate",
            blockers=[
                "full_edge_floor_not_cleared",
                "min_round_trips_not_met",
                "drawdown_gate_failed",
                "research_decision_failed",
                "parity_evidence_missing",
            ],
            full_edge=2.0,
            oos_edge=-1.0,
            full_events=30,
            drawdown_gate=False,
        ),
        _readiness_row(
            "sample_candidate",
            blockers=["min_round_trips_not_met", "parity_evidence_missing"],
            full_events=120,
        ),
    ]
    for row in readiness_rows:
        row["command_families"] = [parity_family]
    readiness = {
        "schema": "research.readiness_summary.v1",
        "total_candidates": len(readiness_rows),
        "rows": readiness_rows,
    }
    advancement = factory._research_candidate_advancement_payload(readiness)

    payload = factory._research_readiness_backfill_queue_payload(readiness, advancement)

    assert payload["status"] == "ready"
    assert payload["errors"] == []
    assert payload["queue_count"] == 1
    assert payload["queue"][0]["candidate"] == "evidence_candidate"
    assert payload["skipped"] == [
        {
            "candidate": "archive_candidate",
            "readiness_status": "not_eligible",
            "advancement_status": "archive_candidate",
            "reason": "advancement_route_not_evidence_backfill",
            "status": "skipped",
        },
        {
            "candidate": "sample_candidate",
            "readiness_status": "not_eligible",
            "advancement_status": "sample_expansion_candidate",
            "reason": "advancement_route_not_evidence_backfill",
            "status": "skipped",
        },
    ]


def test_factory_readiness_backfill_queue_blocks_candidate_identity_mismatch() -> None:
    readiness = {
        "schema": "research.readiness_summary.v1",
        "total_candidates": 1,
        "rows": [_readiness_row("readiness_candidate", blockers=["parity_evidence_missing"])],
    }
    advancement = factory._research_candidate_advancement_payload(
        {
            "schema": "research.readiness_summary.v1",
            "total_candidates": 1,
            "rows": [_readiness_row("advancement_candidate", blockers=["parity_evidence_missing"])],
        }
    )

    payload = factory._research_readiness_backfill_queue_payload(readiness, advancement)

    assert payload["status"] == "blocked"
    assert payload["errors"] == ["readiness_advancement_candidate_set_mismatch"]
    assert payload["queue_count"] == 0
    assert payload["queue"] == []


def test_factory_readiness_backfill_queue_blocks_duplicate_advancement_candidate() -> None:
    readiness = {
        "schema": "research.readiness_summary.v1",
        "total_candidates": 1,
        "rows": [_readiness_row("candidate", blockers=["parity_evidence_missing"])],
    }
    advancement = factory._research_candidate_advancement_payload(readiness)
    advancement["candidates"].append(dict(advancement["candidates"][0]))

    payload = factory._research_readiness_backfill_queue_payload(readiness, advancement)

    assert payload["status"] == "blocked"
    assert payload["errors"] == ["duplicate_advancement_candidate"]
    assert payload["queue"] == []


def test_factory_readiness_backfill_queue_blocks_duplicate_readiness_candidate() -> None:
    readiness = {
        "schema": "research.readiness_summary.v1",
        "total_candidates": 1,
        "rows": [_readiness_row("candidate", blockers=["parity_evidence_missing"])],
    }
    advancement = factory._research_candidate_advancement_payload(readiness)
    readiness["rows"].append(dict(readiness["rows"][0]))

    payload = factory._research_readiness_backfill_queue_payload(readiness, advancement)

    assert payload["status"] == "blocked"
    assert payload["errors"] == ["duplicate_readiness_candidate"]
    assert payload["queue"] == []


def test_factory_readiness_backfill_queue_blocks_invalid_schemas() -> None:
    readiness = {
        "schema": "research.readiness_summary.v0",
        "total_candidates": 1,
        "rows": [_readiness_row("candidate", blockers=["parity_evidence_missing"])],
    }
    advancement = factory._research_candidate_advancement_payload(
        {**readiness, "schema": "research.readiness_summary.v1"}
    )
    advancement["schema"] = "research.readiness_candidate_advancement.v0"

    payload = factory._research_readiness_backfill_queue_payload(readiness, advancement)

    assert payload["status"] == "blocked"
    assert payload["errors"] == ["invalid_readiness_schema", "invalid_advancement_schema"]


def test_factory_readiness_backfill_queue_blocks_empty_candidate_identity() -> None:
    readiness = {
        "schema": "research.readiness_summary.v1",
        "total_candidates": 1,
        "rows": [_readiness_row("", blockers=["parity_evidence_missing"])],
    }
    advancement = factory._research_candidate_advancement_payload(readiness)

    payload = factory._research_readiness_backfill_queue_payload(readiness, advancement)

    assert payload["status"] == "blocked"
    assert payload["errors"] == ["invalid_candidate_identity"]


def test_factory_readiness_backfill_queue_lists_candidate_operator_commands_without_mutation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    spec = _valid_strategy_spec()
    spec["strategy_name"] = "queue_candidate"
    _write_governed_alpha(root, "queue_candidate", spec)

    validation_dir = root / "experiments" / "validations" / "queue_candidate"
    validation_dir.mkdir(parents=True)
    summary_path = validation_dir / "20260604T000000Z_summary.json"
    summary = {
        "candidate": "queue_candidate",
        "artifact_scope": "validation_summary",
        "summary_path": str(summary_path),
        "edge_floor_metric": "mean_net_edge_pts_per_trade",
        "edge_floor_cleared": True,
        "research_decision": {
            "status": "promising",
            "reason": "unit_test_candidate",
            "evidence": ["mean_net_edge_pts_per_trade"],
            "decided_by": "unit_test_gate",
        },
        "hard_gate": {"drawdown_within_2x_average_monthly_net_pnl": True},
        "splits": {
            "full": {
                "events": 360,
                "trading_days": 90,
                "mean_net_edge_pts_per_trade": 18.0,
            },
            "out_of_sample": {
                "events": 120,
                "trading_days": 70,
                "mean_net_edge_pts_per_trade": 12.5,
            },
        },
        "definition": {"cost_pts": 8.0},
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "readiness_backfill_queue.json"
    rc = factory.cmd_readiness_backfill_queue(argparse.Namespace(out=str(out), apply=False))

    assert rc == 0
    assert json.loads(summary_path.read_text(encoding="utf-8")) == summary
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == "research.readiness_backfill_queue.v1"
    assert payload["mode"] == "dry_run"
    assert payload["apply"] is False
    assert payload["queue_count"] == 2
    assert payload["skipped_count"] == 0
    assert payload["queue_counts_by_blocked_gate"] == {
        "evidence_completeness": 1,
        "replay_paper_live_parity": 1,
    }
    assert payload["queue_counts_by_priority"] == {"10": 1, "20": 1}
    assert payload["candidate_queue_counts"] == {"queue_candidate": 2}
    assert payload["candidate_queue_blocked_gates"] == {
        "queue_candidate": [
            "evidence_completeness",
            "replay_paper_live_parity",
        ],
    }
    assert payload["queue"] == [
        {
            "candidate": "queue_candidate",
            "readiness_status": "blocked_by_audit",
            "summary_path": "experiments/validations/queue_candidate/20260604T000000Z_summary.json",
            "spec_path": "alphas/queue_candidate/spec.yaml",
            "reason": "out_of_sample_distribution_evidence_missing",
            "status": "requires_operator_evidence",
            "priority": 10,
            "blocked_gate": "evidence_completeness",
            "candidate_queue_rank": 1,
            "candidate_queue_count": 2,
            "readiness_blockers": ["out_of_sample_distribution_evidence_missing"],
            "readiness_next_actions": [
                "review_out_of_sample_distribution_dominance",
                "provide_or_attach_replay_paper_live_parity_evidence",
            ],
            "command_family": "oos_distribution_evidence",
            "attach_target": "validation_summary.splits.out_of_sample",
            "operator_commands": [
                "oos-distribution-evidence-template",
                "oos-distribution-evidence-validate",
                "oos-distribution-evidence-attach",
            ],
        },
        {
            "candidate": "queue_candidate",
            "readiness_status": "blocked_by_audit",
            "summary_path": "experiments/validations/queue_candidate/20260604T000000Z_summary.json",
            "spec_path": "alphas/queue_candidate/spec.yaml",
            "reason": "parity_evidence_missing",
            "status": "requires_operator_evidence",
            "priority": 20,
            "blocked_gate": "replay_paper_live_parity",
            "candidate_queue_rank": 2,
            "candidate_queue_count": 2,
            "readiness_blockers": ["parity_evidence_missing"],
            "readiness_next_actions": [
                "review_out_of_sample_distribution_dominance",
                "provide_or_attach_replay_paper_live_parity_evidence",
            ],
            "command_family": "parity_evidence",
            "attach_target": "validation_summary.parity_evidence",
            "operator_commands": [
                "parity-evidence-template",
                "parity-evidence-validate",
                "parity-evidence-attach",
            ],
        },
    ]
    assert payload["skipped"] == []


def test_factory_research_decision_replay_includes_readiness_blocker_provenance(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    spec = _valid_strategy_spec()
    spec["strategy_name"] = "replay_blocked_candidate"
    _write_governed_alpha(root, "replay_blocked_candidate", spec)

    validation_dir = root / "experiments" / "validations" / "replay_blocked_candidate"
    validation_dir.mkdir(parents=True)
    summary_path = validation_dir / "20260604T000000Z_summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "candidate": "replay_blocked_candidate",
                "artifact_scope": "validation_summary",
                "summary_path": str(summary_path),
                "edge_floor_metric": "mean_net_edge_pts_per_trade",
                "edge_floor_cleared": False,
                "research_decision": {
                    "status": "failed",
                    "reason": "edge_and_sample_failed",
                    "evidence": ["mean_net_edge_pts_per_trade", "min_sample_size"],
                    "decided_by": "unit_test_gate",
                },
                "hard_gate": {"drawdown_within_2x_average_monthly_net_pnl": False},
                "splits": {
                    "full": {
                        "events": 120,
                        "trading_days": 45,
                        "mean_net_edge_pts_per_trade": 8.0,
                        "max_drawdown_net_pts": 30.0,
                        "average_monthly_net_pnl": 10.0,
                        "median_monthly_net_pnl": 9.0,
                        "worst_month_net_pnl": -8.0,
                        "drawdown_within_2x_average_monthly_net_pnl": False,
                    },
                    "out_of_sample": {
                        "events": 40,
                        "trading_days": 20,
                        "mean_net_edge_pts_per_trade": 7.0,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "audit_decision_replay.json"
    rc = factory.cmd_audit(argparse.Namespace(out=str(out), fail_on_warning=False, data=[]))

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["details"]["research_decision_replay"] == [
        {
            "candidate": "replay_blocked_candidate",
            "replay_status": "traceable",
            "status": "failed",
            "reason": "edge_and_sample_failed",
            "summary_path": "experiments/validations/replay_blocked_candidate/20260604T000000Z_summary.json",
            "spec_path": "alphas/replay_blocked_candidate/spec.yaml",
            "readiness_status": "failed",
            "paper_live_eligible": False,
            "primary_blocker": "full_edge_floor_not_cleared",
            "blockers": [
                "full_edge_floor_not_cleared",
                "out_of_sample_edge_floor_not_cleared",
                "min_round_trips_not_met",
                "min_oos_trading_days_not_met",
                "drawdown_gate_failed",
                "out_of_sample_distribution_evidence_missing",
                "research_decision_failed",
                "parity_evidence_missing",
            ],
            "next_actions": [
                "retain_failed_research_record",
                "stop_or_form_new_hypothesis_after_edge_failure",
                "collect_more_sample_before_completion",
                "review_drawdown_monthly_distribution",
                "review_out_of_sample_distribution_dominance",
                "provide_or_attach_replay_paper_live_parity_evidence",
            ],
            "command_families": [
                {
                    "blocker": "out_of_sample_distribution_evidence_missing",
                    "command_family": "oos_distribution_evidence",
                    "attach_target": "validation_summary.splits.out_of_sample",
                    "commands": [
                        "oos-distribution-evidence-backfill-plan",
                        "oos-distribution-evidence-template",
                        "oos-distribution-evidence-validate",
                        "oos-distribution-evidence-attach",
                    ],
                },
                {
                    "blocker": "parity_evidence_missing",
                    "command_family": "parity_evidence",
                    "attach_target": "validation_summary.parity_evidence",
                    "commands": [
                        "parity-evidence-backfill-plan",
                        "parity-evidence-template",
                        "parity-evidence-validate",
                        "parity-evidence-attach",
                    ],
                },
            ],
            "edge_floor_metric": "mean_net_edge_pts_per_trade",
            "mean_net_edge_pts_per_trade": 8.0,
            "edge_floor_cleared": False,
            "out_of_sample_mean_net_edge_pts_per_trade": 7.0,
            "risk_gate_drawdown_within_2x_average_monthly_net_pnl": False,
            "full_max_drawdown_net_pts": 30.0,
            "full_average_monthly_net_pnl": 10.0,
            "full_worst_month_net_pnl": -8.0,
            "traceability_missing": [],
        }
    ]


def test_factory_audit_classifies_parity_evidence_schema_and_mismatch_categories(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    spec = _valid_strategy_spec()
    spec["strategy_name"] = "parity_invalid"
    _write_governed_alpha(root, "parity_invalid", spec)

    validation_dir = root / "experiments" / "validations" / "parity_invalid"
    validation_dir.mkdir(parents=True)
    summary_path = validation_dir / "20260603T000000Z_summary.json"
    summary = {
        "candidate": "parity_invalid",
        "artifact_scope": "validation_summary",
        "summary_path": str(summary_path),
        "edge_floor_metric": "mean_net_edge_pts_per_trade",
        "edge_floor_cleared": True,
        "research_decision": {
            "status": "promising",
            "reason": "unit_test_candidate",
            "evidence": ["mean_net_edge_pts_per_trade"],
            "decided_by": "unit_test_gate",
        },
        "hard_gate": {"drawdown_within_2x_average_monthly_net_pnl": True},
        "splits": {
            "full": {
                "events": 360,
                "trading_days": 90,
                "mean_net_edge_pts_per_trade": 18.0,
            },
            "out_of_sample": {
                "events": 120,
                "trading_days": 70,
                "mean_net_edge_pts_per_trade": 12.5,
            },
        },
        "definition": {"cost_pts": 8.0},
        "parity_evidence": {
            "artifact_scope": "parity_evidence",
            "match_pct": 90.0,
            "threshold": 95.0,
            "checked_dimensions": [
                "signal_trigger_time",
                "direction",
                "position_size",
                "entry",
            ],
            "mismatch_counts": {
                "latency_shift": 2,
                "made_up_label": 1,
            },
        },
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "audit_parity_schema.json"
    rc = factory.cmd_audit(argparse.Namespace(out=str(out), fail_on_warning=False, data=[]))

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    parity = payload["details"]["research_parity_evidence"]
    assert parity["allowed_mismatch_categories"] == [
        "data_mismatch",
        "feature_mismatch",
        "timestamp_alignment_error",
        "latency_shift",
        "session_phase_filter",
        "risk_filter",
        "position_limit",
        "implementation_drift",
        "unknown",
    ]
    assert parity["invalid"] == [
        {
            "candidate": "parity_invalid",
            "summary_path": "experiments/validations/parity_invalid/20260603T000000Z_summary.json",
            "status": "invalid",
            "match_pct": 90.0,
            "threshold": 95.0,
            "mismatch_counts": {"latency_shift": 2, "made_up_label": 1},
            "invalid_mismatch_categories": ["made_up_label"],
            "missing_checks": [
                "exit",
                "session_filter",
                "risk_filter",
                "force_flat_rule",
            ],
            "errors": [
                "match_pct_below_threshold",
                "missing_required_checks",
                "invalid_mismatch_categories",
            ],
        }
    ]
    row = payload["details"]["research_candidate_comparison"]["rows"][0]
    assert row["parity_evidence_status"] == "invalid"
    assert row["replay_match_pct"] == 90.0
    assert "parity_evidence_invalid" in row["blockers"]


def test_factory_parity_evidence_template_writes_canonical_payload(tmp_path: Path) -> None:
    out = tmp_path / "parity_evidence.json"

    rc = factory.cmd_parity_evidence_template(
        argparse.Namespace(
            candidate="candidate_keep",
            summary_path="experiments/validations/candidate_keep/20260603T000000Z_summary.json",
            match_pct=96.0,
            threshold=95.0,
            checked_dimension=[],
            mismatch_count=["latency_shift=1"],
            out=str(out),
        )
    )

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "evidence"
    assert payload["candidate"] == "candidate_keep"
    assert payload["schema"] == "research.parity_evidence.v1"
    assert payload["parity_evidence"] == {
        "artifact_scope": "parity_evidence",
        "match_pct": 96.0,
        "threshold": 95.0,
        "checked_dimensions": [
            "signal_trigger_time",
            "direction",
            "position_size",
            "entry",
            "exit",
            "session_filter",
            "risk_filter",
            "force_flat_rule",
        ],
        "mismatch_counts": {"latency_shift": 1},
    }
    assert payload["validation"] == {
        "candidate": "candidate_keep",
        "summary_path": "experiments/validations/candidate_keep/20260603T000000Z_summary.json",
        "status": "pass",
        "match_pct": 96.0,
        "threshold": 95.0,
        "mismatch_counts": {"latency_shift": 1},
        "invalid_mismatch_categories": [],
        "missing_checks": [],
        "errors": [],
    }


def test_factory_parity_evidence_template_refuses_invalid_mismatch_category(tmp_path: Path) -> None:
    out = tmp_path / "parity_invalid.json"

    rc = factory.cmd_parity_evidence_template(
        argparse.Namespace(
            candidate="candidate_bad",
            summary_path="experiments/validations/candidate_bad/20260603T000000Z_summary.json",
            match_pct=96.0,
            threshold=95.0,
            checked_dimension=[],
            mismatch_count=["made_up_label=1"],
            out=str(out),
        )
    )

    assert rc == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["validation"]["status"] == "invalid"
    assert payload["validation"]["invalid_mismatch_categories"] == ["made_up_label"]
    assert payload["validation"]["errors"] == ["invalid_mismatch_categories"]


def test_factory_parity_evidence_backfill_plan_lists_missing_evidence_without_mutation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    spec = _valid_strategy_spec()
    spec["strategy_name"] = "missing_parity_alpha"
    _write_governed_alpha(root, "missing_parity_alpha", spec)

    validation_dir = root / "experiments" / "validations" / "missing_parity_alpha"
    validation_dir.mkdir(parents=True)
    summary_path = validation_dir / "20260604T000000Z_summary.json"
    summary = {
        "candidate": "missing_parity_alpha",
        "artifact_scope": "validation_summary",
        "summary_path": str(summary_path),
        "edge_floor_metric": "mean_net_edge_pts_per_trade",
        "edge_floor_cleared": True,
        "research_decision": {
            "status": "promising",
            "reason": "unit_test_candidate",
            "evidence": ["mean_net_edge_pts_per_trade"],
            "decided_by": "unit_test_gate",
        },
        "hard_gate": {"drawdown_within_2x_average_monthly_net_pnl": True},
        "splits": {
            "full": {
                "events": 360,
                "trading_days": 90,
                "mean_net_edge_pts_per_trade": 18.0,
            },
            "out_of_sample": {
                "events": 120,
                "trading_days": 70,
                "mean_net_edge_pts_per_trade": 12.5,
            },
        },
        "definition": {"cost_pts": 8.0},
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "parity_backfill_plan.json"
    rc = factory.cmd_parity_evidence_backfill_plan(argparse.Namespace(out=str(out), apply=False))

    assert rc == 0
    assert json.loads(summary_path.read_text(encoding="utf-8")) == summary
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "dry_run"
    assert payload["apply"] is False
    assert payload["planned_count"] == 1
    assert payload["planned"] == [
        {
            "candidate": "missing_parity_alpha",
            "summary_path": "experiments/validations/missing_parity_alpha/20260604T000000Z_summary.json",
            "reason": "parity_evidence_missing",
            "status": "requires_operator_evidence",
            "readiness_blockers": ["parity_evidence_missing"],
            "readiness_next_actions": ["provide_or_attach_replay_paper_live_parity_evidence"],
            "attach_target": "validation_summary.parity_evidence",
            "operator_commands": [
                "parity-evidence-template",
                "parity-evidence-validate",
                "parity-evidence-attach",
            ],
            "required_checks": [
                "signal_trigger_time",
                "direction",
                "position_size",
                "entry",
                "exit",
                "session_filter",
                "risk_filter",
                "force_flat_rule",
            ],
            "allowed_mismatch_categories": [
                "data_mismatch",
                "feature_mismatch",
                "timestamp_alignment_error",
                "latency_shift",
                "session_phase_filter",
                "risk_filter",
                "position_limit",
                "implementation_drift",
                "unknown",
            ],
            "parity_evidence_template": {
                "artifact_scope": "parity_evidence",
                "match_pct": None,
                "threshold": 95.0,
                "checked_dimensions": [
                    "signal_trigger_time",
                    "direction",
                    "position_size",
                    "entry",
                    "exit",
                    "session_filter",
                    "risk_filter",
                    "force_flat_rule",
                ],
                "mismatch_counts": {},
            },
        }
    ]
    assert payload["skipped"] == []


def test_factory_parity_evidence_validate_marks_operator_evidence_ready_without_mutation(
    tmp_path: Path,
) -> None:
    summary_path = tmp_path / "20260604T000000Z_summary.json"
    summary = {
        "candidate": "candidate_keep",
        "artifact_scope": "validation_summary",
        "parity_evidence": None,
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    evidence_path = tmp_path / "operator_parity_evidence.json"
    evidence = {
        "schema": "research.parity_evidence.v1",
        "candidate": "candidate_keep",
        "summary_path": str(summary_path),
        "parity_evidence": {
            "artifact_scope": "parity_evidence",
            "match_pct": 97.5,
            "threshold": 95.0,
            "checked_dimensions": [
                "signal_trigger_time",
                "direction",
                "position_size",
                "entry",
                "exit",
                "session_filter",
                "risk_filter",
                "force_flat_rule",
            ],
            "mismatch_counts": {"latency_shift": 2},
        },
    }
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    out = tmp_path / "validated_parity_evidence.json"

    rc = factory.cmd_parity_evidence_validate(
        argparse.Namespace(evidence=str(evidence_path), out=str(out), candidate="", summary_path="")
    )

    assert rc == 0
    assert json.loads(summary_path.read_text(encoding="utf-8")) == summary
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == "research.parity_evidence.validation.v1"
    assert payload["mode"] == "validated_evidence"
    assert payload["evidence_path"] == str(evidence_path.resolve())
    assert payload["candidate"] == "candidate_keep"
    assert payload["summary_path"] == str(summary_path)
    assert payload["validation"]["status"] == "pass"
    assert payload["attachment"] == {
        "target": "validation_summary.parity_evidence",
        "status": "ready_to_attach",
        "mutates_summary": False,
        "errors": [],
    }


def test_factory_parity_evidence_validate_blocks_incomplete_operator_evidence(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "operator_parity_evidence_invalid.json"
    evidence = {
        "schema": "research.parity_evidence.v1",
        "candidate": "candidate_blocked",
        "summary_path": "experiments/validations/candidate_blocked/20260604T000000Z_summary.json",
        "parity_evidence": {
            "artifact_scope": "parity_evidence",
            "match_pct": 91.0,
            "threshold": 95.0,
            "checked_dimensions": [
                "signal_trigger_time",
                "direction",
                "position_size",
                "entry",
            ],
            "mismatch_counts": {"unknown": 1},
        },
    }
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    out = tmp_path / "validated_parity_evidence_invalid.json"

    rc = factory.cmd_parity_evidence_validate(
        argparse.Namespace(evidence=str(evidence_path), out=str(out), candidate="", summary_path="")
    )

    assert rc == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["validation"]["status"] == "invalid"
    assert payload["validation"]["errors"] == [
        "match_pct_below_threshold",
        "missing_required_checks",
    ]
    assert payload["validation"]["missing_checks"] == [
        "exit",
        "session_filter",
        "risk_filter",
        "force_flat_rule",
    ]
    assert payload["attachment"] == {
        "target": "validation_summary.parity_evidence",
        "status": "blocked",
        "mutates_summary": False,
        "errors": ["parity_evidence_invalid"],
    }


def test_factory_parity_evidence_attach_dry_run_keeps_summary_unchanged(
    tmp_path: Path,
) -> None:
    summary_path = tmp_path / "20260604T000000Z_summary.json"
    summary = {
        "candidate": "candidate_keep",
        "artifact_scope": "validation_summary",
        "research_decision": {"status": "promising", "reason": "unit_test"},
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    parity_evidence = {
        "artifact_scope": "parity_evidence",
        "match_pct": 97.5,
        "threshold": 95.0,
        "checked_dimensions": [
            "signal_trigger_time",
            "direction",
            "position_size",
            "entry",
            "exit",
            "session_filter",
            "risk_filter",
            "force_flat_rule",
        ],
        "mismatch_counts": {"latency_shift": 2},
    }
    validation_path = tmp_path / "validated_parity_evidence.json"
    validation_path.write_text(
        json.dumps(
            factory._parity_evidence_validation_artifact(
                evidence_path=tmp_path / "operator_parity_evidence.json",
                candidate="candidate_keep",
                summary_path=str(summary_path),
                parity_evidence=parity_evidence,
            )
        ),
        encoding="utf-8",
    )
    out = tmp_path / "attach_plan.json"

    rc = factory.cmd_parity_evidence_attach(
        argparse.Namespace(validation=str(validation_path), out=str(out), apply=False)
    )

    assert rc == 0
    assert json.loads(summary_path.read_text(encoding="utf-8")) == summary
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == "research.parity_evidence.attach.v1"
    assert payload["mode"] == "dry_run"
    assert payload["apply"] is False
    assert payload["status"] == "ready_to_apply"
    assert payload["mutates_summary"] is False
    assert payload["errors"] == []
    assert payload["planned_update"] == {"parity_evidence": parity_evidence}


def test_factory_parity_evidence_attach_apply_writes_only_parity_evidence(
    tmp_path: Path,
) -> None:
    summary_path = tmp_path / "20260604T000000Z_summary.json"
    summary = {
        "candidate": "candidate_apply",
        "artifact_scope": "validation_summary",
        "research_decision": {"status": "promising", "reason": "unit_test"},
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    parity_evidence = {
        "artifact_scope": "parity_evidence",
        "match_pct": 96.0,
        "threshold": 95.0,
        "checked_dimensions": [
            "signal_trigger_time",
            "direction",
            "position_size",
            "entry",
            "exit",
            "session_filter",
            "risk_filter",
            "force_flat_rule",
        ],
        "mismatch_counts": {"unknown": 1},
    }
    validation_path = tmp_path / "validated_parity_evidence_apply.json"
    validation_path.write_text(
        json.dumps(
            factory._parity_evidence_validation_artifact(
                evidence_path=tmp_path / "operator_parity_evidence_apply.json",
                candidate="candidate_apply",
                summary_path=str(summary_path),
                parity_evidence=parity_evidence,
            )
        ),
        encoding="utf-8",
    )
    out = tmp_path / "attach_apply.json"

    rc = factory.cmd_parity_evidence_attach(
        argparse.Namespace(validation=str(validation_path), out=str(out), apply=True)
    )

    assert rc == 0
    assert json.loads(summary_path.read_text(encoding="utf-8")) == {
        **summary,
        "parity_evidence": parity_evidence,
    }
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "apply"
    assert payload["status"] == "applied"
    assert payload["mutates_summary"] is True
    assert payload["errors"] == []


def test_factory_parity_evidence_attach_blocks_invalid_or_mismatched_artifact(
    tmp_path: Path,
) -> None:
    summary_path = tmp_path / "20260604T000000Z_summary.json"
    summary = {"candidate": "candidate_keep", "artifact_scope": "validation_summary"}
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    validation_path = tmp_path / "validated_parity_evidence_blocked.json"
    validation_path.write_text(
        json.dumps(
            {
                "schema": "research.parity_evidence.validation.v1",
                "candidate": "candidate_other",
                "summary_path": str(summary_path),
                "parity_evidence": {},
                "validation": {"status": "invalid"},
                "attachment": {
                    "target": "validation_summary.parity_evidence",
                    "status": "blocked",
                    "mutates_summary": False,
                    "errors": ["parity_evidence_invalid"],
                },
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "attach_blocked.json"

    rc = factory.cmd_parity_evidence_attach(
        argparse.Namespace(validation=str(validation_path), out=str(out), apply=True)
    )

    assert rc == 1
    assert json.loads(summary_path.read_text(encoding="utf-8")) == summary
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "blocked"
    assert payload["mutates_summary"] is False
    assert payload["errors"] == [
        "validated_attachment_not_ready",
        "validated_parity_evidence_not_pass",
        "candidate_mismatch",
    ]


def test_factory_oos_distribution_evidence_template_writes_canonical_payload(tmp_path: Path) -> None:
    out = tmp_path / "oos_distribution_evidence.json"

    rc = factory.cmd_oos_distribution_evidence_template(
        argparse.Namespace(
            candidate="candidate_keep",
            summary_path="experiments/validations/candidate_keep/20260603T000000Z_summary.json",
            pnl_distribution="pass",
            loss_distribution="pass",
            single_trade_dominance="pass",
            single_day_dominance="fail",
            out=str(out),
        )
    )

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == "research.oos_distribution_evidence.v1"
    assert payload["mode"] == "evidence"
    assert payload["candidate"] == "candidate_keep"
    assert payload["summary_path"] == "experiments/validations/candidate_keep/20260603T000000Z_summary.json"
    assert payload["oos_distribution_evidence"] == {
        "artifact_scope": "oos_distribution_evidence",
        "pnl_distribution_checked": True,
        "loss_distribution_checked": True,
        "single_trade_dominance_passed": True,
        "single_day_dominance_passed": False,
    }
    assert payload["validation"] == {
        "candidate": "candidate_keep",
        "summary_path": "experiments/validations/candidate_keep/20260603T000000Z_summary.json",
        "status": "pass",
        "evidence_passed": False,
        "missing_fields": [],
        "errors": [],
    }


def test_factory_oos_distribution_evidence_validate_marks_ready_without_mutation(
    tmp_path: Path,
) -> None:
    summary_path = tmp_path / "20260604T000000Z_summary.json"
    summary = {
        "candidate": "candidate_keep",
        "artifact_scope": "validation_summary",
        "splits": {
            "full": {"events": 360},
            "out_of_sample": {"events": 120, "trading_days": 70},
        },
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    evidence_path = tmp_path / "operator_oos_distribution_evidence.json"
    evidence = {
        "schema": "research.oos_distribution_evidence.v1",
        "candidate": "candidate_keep",
        "summary_path": str(summary_path),
        "oos_distribution_evidence": {
            "artifact_scope": "oos_distribution_evidence",
            "pnl_distribution_checked": True,
            "loss_distribution_checked": True,
            "single_trade_dominance_passed": True,
            "single_day_dominance_passed": True,
        },
    }
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    out = tmp_path / "validated_oos_distribution_evidence.json"

    rc = factory.cmd_oos_distribution_evidence_validate(
        argparse.Namespace(evidence=str(evidence_path), out=str(out), candidate="", summary_path="")
    )

    assert rc == 0
    assert json.loads(summary_path.read_text(encoding="utf-8")) == summary
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == "research.oos_distribution_evidence.validation.v1"
    assert payload["mode"] == "validated_evidence"
    assert payload["evidence_path"] == str(evidence_path.resolve())
    assert payload["candidate"] == "candidate_keep"
    assert payload["summary_path"] == str(summary_path)
    assert payload["validation"] == {
        "candidate": "candidate_keep",
        "summary_path": str(summary_path),
        "status": "pass",
        "evidence_passed": True,
        "missing_fields": [],
        "errors": [],
    }
    assert payload["attachment"] == {
        "target": "validation_summary.splits.out_of_sample",
        "status": "ready_to_attach",
        "mutates_summary": False,
        "errors": [],
    }


def test_factory_oos_distribution_evidence_validate_blocks_missing_fields(
    tmp_path: Path,
) -> None:
    evidence_path = tmp_path / "operator_oos_distribution_evidence_invalid.json"
    evidence = {
        "schema": "research.oos_distribution_evidence.v1",
        "candidate": "candidate_blocked",
        "summary_path": "experiments/validations/candidate_blocked/20260604T000000Z_summary.json",
        "oos_distribution_evidence": {
            "artifact_scope": "oos_distribution_evidence",
            "pnl_distribution_checked": True,
            "single_trade_dominance_passed": True,
        },
    }
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    out = tmp_path / "validated_oos_distribution_evidence_invalid.json"

    rc = factory.cmd_oos_distribution_evidence_validate(
        argparse.Namespace(evidence=str(evidence_path), out=str(out), candidate="", summary_path="")
    )

    assert rc == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["validation"]["status"] == "invalid"
    assert payload["validation"]["missing_fields"] == [
        "loss_distribution_checked",
        "single_day_dominance_passed",
    ]
    assert payload["validation"]["errors"] == ["missing_required_fields"]
    assert payload["attachment"] == {
        "target": "validation_summary.splits.out_of_sample",
        "status": "blocked",
        "mutates_summary": False,
        "errors": ["oos_distribution_evidence_invalid"],
    }


def test_factory_oos_distribution_evidence_attach_apply_writes_out_of_sample_fields(
    tmp_path: Path,
) -> None:
    summary_path = tmp_path / "20260604T000000Z_summary.json"
    summary = {
        "candidate": "candidate_apply",
        "artifact_scope": "validation_summary",
        "splits": {
            "full": {"events": 360},
            "out_of_sample": {"events": 120, "trading_days": 70},
        },
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    oos_evidence = {
        "artifact_scope": "oos_distribution_evidence",
        "pnl_distribution_checked": True,
        "loss_distribution_checked": False,
        "single_trade_dominance_passed": True,
        "single_day_dominance_passed": False,
    }
    validation_path = tmp_path / "validated_oos_distribution_evidence_apply.json"
    validation_path.write_text(
        json.dumps(
            factory._oos_distribution_evidence_validation_artifact(
                evidence_path=tmp_path / "operator_oos_distribution_evidence_apply.json",
                candidate="candidate_apply",
                summary_path=str(summary_path),
                oos_distribution_evidence=oos_evidence,
            )
        ),
        encoding="utf-8",
    )
    out = tmp_path / "attach_oos_distribution_apply.json"

    rc = factory.cmd_oos_distribution_evidence_attach(
        argparse.Namespace(validation=str(validation_path), out=str(out), apply=True)
    )

    assert rc == 0
    applied_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert applied_summary == {
        **summary,
        "splits": {
            "full": {"events": 360},
            "out_of_sample": {
                "events": 120,
                "trading_days": 70,
                "pnl_distribution_checked": True,
                "loss_distribution_checked": False,
                "single_trade_dominance_passed": True,
                "single_day_dominance_passed": False,
            },
        },
    }
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == "research.oos_distribution_evidence.attach.v1"
    assert payload["mode"] == "apply"
    assert payload["status"] == "applied"
    assert payload["mutates_summary"] is True
    assert payload["errors"] == []


def test_factory_oos_distribution_evidence_attach_blocks_existing_fields(
    tmp_path: Path,
) -> None:
    summary_path = tmp_path / "20260604T000000Z_summary.json"
    summary = {
        "candidate": "candidate_keep",
        "artifact_scope": "validation_summary",
        "splits": {
            "out_of_sample": {
                "events": 120,
                "pnl_distribution_checked": True,
            }
        },
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    oos_evidence = {
        "artifact_scope": "oos_distribution_evidence",
        "pnl_distribution_checked": True,
        "loss_distribution_checked": True,
        "single_trade_dominance_passed": True,
        "single_day_dominance_passed": True,
    }
    validation_path = tmp_path / "validated_oos_distribution_evidence_blocked.json"
    validation_path.write_text(
        json.dumps(
            factory._oos_distribution_evidence_validation_artifact(
                evidence_path=tmp_path / "operator_oos_distribution_evidence_blocked.json",
                candidate="candidate_keep",
                summary_path=str(summary_path),
                oos_distribution_evidence=oos_evidence,
            )
        ),
        encoding="utf-8",
    )
    out = tmp_path / "attach_oos_distribution_blocked.json"

    rc = factory.cmd_oos_distribution_evidence_attach(
        argparse.Namespace(validation=str(validation_path), out=str(out), apply=True)
    )

    assert rc == 1
    assert json.loads(summary_path.read_text(encoding="utf-8")) == summary
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "blocked"
    assert payload["mutates_summary"] is False
    assert payload["errors"] == ["summary_already_has_oos_distribution_evidence"]


def test_factory_oos_distribution_evidence_backfill_plan_lists_missing_without_mutation(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    spec = _valid_strategy_spec()
    spec["strategy_name"] = "missing_oos_distribution_alpha"
    _write_governed_alpha(root, "missing_oos_distribution_alpha", spec)

    validation_dir = root / "experiments" / "validations" / "missing_oos_distribution_alpha"
    validation_dir.mkdir(parents=True)
    summary_path = validation_dir / "20260604T000000Z_summary.json"
    summary = {
        "candidate": "missing_oos_distribution_alpha",
        "artifact_scope": "validation_summary",
        "summary_path": str(summary_path),
        "edge_floor_metric": "mean_net_edge_pts_per_trade",
        "edge_floor_cleared": True,
        "research_decision": {
            "status": "promising",
            "reason": "unit_test_candidate",
            "evidence": ["mean_net_edge_pts_per_trade"],
            "decided_by": "unit_test_gate",
        },
        "hard_gate": {"drawdown_within_2x_average_monthly_net_pnl": True},
        "splits": {
            "full": {
                "events": 360,
                "trading_days": 90,
                "mean_net_edge_pts_per_trade": 18.0,
            },
            "out_of_sample": {
                "events": 120,
                "trading_days": 70,
                "mean_net_edge_pts_per_trade": 12.5,
            },
        },
        "definition": {"cost_pts": 8.0},
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "oos_distribution_backfill_plan.json"
    rc = factory.cmd_oos_distribution_evidence_backfill_plan(argparse.Namespace(out=str(out), apply=False))

    assert rc == 0
    assert json.loads(summary_path.read_text(encoding="utf-8")) == summary
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "dry_run"
    assert payload["apply"] is False
    assert payload["planned_count"] == 1
    assert payload["skipped_count"] == 0
    assert payload["planned"] == [
        {
            "candidate": "missing_oos_distribution_alpha",
            "summary_path": ("experiments/validations/missing_oos_distribution_alpha/20260604T000000Z_summary.json"),
            "reason": "out_of_sample_distribution_evidence_missing",
            "status": "requires_operator_evidence",
            "readiness_blockers": ["out_of_sample_distribution_evidence_missing"],
            "readiness_next_actions": ["review_out_of_sample_distribution_dominance"],
            "attach_target": "validation_summary.splits.out_of_sample",
            "operator_commands": [
                "oos-distribution-evidence-template",
                "oos-distribution-evidence-validate",
                "oos-distribution-evidence-attach",
            ],
            "required_fields": [
                "pnl_distribution_checked",
                "loss_distribution_checked",
                "single_trade_dominance_passed",
                "single_day_dominance_passed",
            ],
            "missing_fields": [
                "pnl_distribution_checked",
                "loss_distribution_checked",
                "single_trade_dominance_passed",
                "single_day_dominance_passed",
            ],
            "oos_distribution_evidence_template": {
                "artifact_scope": "oos_distribution_evidence",
                "pnl_distribution_checked": None,
                "loss_distribution_checked": None,
                "single_trade_dominance_passed": None,
                "single_day_dominance_passed": None,
            },
        }
    ]
    assert payload["skipped"] == []


def test_factory_oos_distribution_evidence_backfill_plan_skips_complete_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    spec = _valid_strategy_spec()
    spec["strategy_name"] = "complete_oos_distribution_alpha"
    _write_governed_alpha(root, "complete_oos_distribution_alpha", spec)

    validation_dir = root / "experiments" / "validations" / "complete_oos_distribution_alpha"
    validation_dir.mkdir(parents=True)
    summary_path = validation_dir / "20260604T000000Z_summary.json"
    summary = {
        "candidate": "complete_oos_distribution_alpha",
        "artifact_scope": "validation_summary",
        "summary_path": str(summary_path),
        "edge_floor_metric": "mean_net_edge_pts_per_trade",
        "edge_floor_cleared": True,
        "research_decision": {
            "status": "promising",
            "reason": "unit_test_candidate",
            "evidence": ["mean_net_edge_pts_per_trade"],
            "decided_by": "unit_test_gate",
        },
        "hard_gate": {"drawdown_within_2x_average_monthly_net_pnl": True},
        "splits": {
            "full": {
                "events": 360,
                "mean_net_edge_pts_per_trade": 18.0,
            },
            "out_of_sample": {
                "events": 120,
                "trading_days": 70,
                "mean_net_edge_pts_per_trade": 12.5,
                "pnl_distribution_checked": True,
                "loss_distribution_checked": True,
                "single_trade_dominance_passed": True,
                "single_day_dominance_passed": True,
            },
        },
    }
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "oos_distribution_backfill_plan_complete.json"
    rc = factory.cmd_oos_distribution_evidence_backfill_plan(argparse.Namespace(out=str(out), apply=False))

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["planned_count"] == 0
    assert payload["skipped_count"] == 1
    assert payload["planned"] == []
    assert payload["skipped"] == [
        {
            "candidate": "complete_oos_distribution_alpha",
            "summary_path": ("experiments/validations/complete_oos_distribution_alpha/20260604T000000Z_summary.json"),
            "reason": "out_of_sample_distribution_evidence_complete",
            "status": "complete",
        }
    ]


def test_factory_parser_exposes_oos_distribution_evidence_commands() -> None:
    parser = factory.build_parser()
    template_args = parser.parse_args(
        [
            "oos-distribution-evidence-template",
            "--candidate",
            "candidate_keep",
            "--summary-path",
            "summary.json",
            "--pnl-distribution",
            "pass",
            "--loss-distribution",
            "pass",
            "--single-trade-dominance",
            "pass",
            "--single-day-dominance",
            "fail",
            "--out",
            "evidence.json",
        ]
    )
    assert template_args.func is factory.cmd_oos_distribution_evidence_template
    assert template_args.single_day_dominance == "fail"

    validate_args = parser.parse_args(
        ["oos-distribution-evidence-validate", "--evidence", "evidence.json", "--out", "validated.json"]
    )
    assert validate_args.func is factory.cmd_oos_distribution_evidence_validate

    attach_args = parser.parse_args(
        ["oos-distribution-evidence-attach", "--validation", "validated.json", "--apply", "--out", "attach.json"]
    )
    assert attach_args.func is factory.cmd_oos_distribution_evidence_attach
    assert attach_args.apply is True


def test_factory_parser_exposes_oos_distribution_evidence_backfill_plan() -> None:
    parser = factory.build_parser()
    args = parser.parse_args(["oos-distribution-evidence-backfill-plan", "--out", "plan.json"])

    assert args.func is factory.cmd_oos_distribution_evidence_backfill_plan
    assert args.out == "plan.json"


def test_factory_parser_exposes_parity_evidence_backfill_plan() -> None:
    parser = factory.build_parser()
    args = parser.parse_args(["parity-evidence-backfill-plan", "--out", "plan.json"])

    assert args.func is factory.cmd_parity_evidence_backfill_plan
    assert args.out == "plan.json"


def test_factory_parser_exposes_parity_evidence_validate() -> None:
    parser = factory.build_parser()
    args = parser.parse_args(["parity-evidence-validate", "--evidence", "evidence.json", "--out", "validated.json"])

    assert args.func is factory.cmd_parity_evidence_validate
    assert args.evidence == "evidence.json"
    assert args.out == "validated.json"


def test_factory_parser_exposes_parity_evidence_attach() -> None:
    parser = factory.build_parser()
    args = parser.parse_args(["parity-evidence-attach", "--validation", "validated.json", "--out", "attach.json"])

    assert args.func is factory.cmd_parity_evidence_attach
    assert args.validation == "validated.json"
    assert args.out == "attach.json"
    assert args.apply is False


def test_factory_parser_exposes_readiness_summary() -> None:
    parser = factory.build_parser()
    args = parser.parse_args(["readiness-summary", "--out", "readiness.json"])

    assert args.func is factory.cmd_readiness_summary
    assert args.out == "readiness.json"


def test_factory_parser_exposes_readiness_backfill_queue() -> None:
    parser = factory.build_parser()
    args = parser.parse_args(["readiness-backfill-queue", "--out", "queue.json"])

    assert args.func is factory.cmd_readiness_backfill_queue
    assert args.out == "queue.json"
    assert args.apply is False


def test_factory_strategy_family_intake_template_covers_futures_options_shapes(tmp_path: Path) -> None:
    out = tmp_path / "strategy_family_intake.json"

    rc = factory.cmd_strategy_family_intake_template(argparse.Namespace(out=str(out)))

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == "research.strategy_family_intake.v1"
    assert payload["required_spec_fields"] == [
        "strategy_name",
        "market",
        "instrument",
        "hypothesis",
        "timeframe",
        "holding_period",
        "frequency_class",
        "entry_rule",
        "exit_rule",
        "position_sizing",
        "risk_control",
        "cost_model",
        "validation_plan",
    ]
    assert payload["allowed_strategy_families"] == [
        "futures_directional",
        "futures_single_leg",
        "futures_multi_leg",
        "futures_spread",
        "options_single_leg",
        "options_multi_leg",
        "options_spread",
        "options_straddle",
        "options_strangle",
        "options_calendar_spread",
        "options_greeks",
    ]
    assert payload["family_shape_requirements"]["options_straddle"] == {
        "instrument_shape": "legs",
        "required_optional_blocks": ["legs"],
        "minimum_legs": 2,
        "notes": [
            "same_expiry",
            "same_strike",
            "one_call_and_one_put",
        ],
    }
    assert payload["family_shape_requirements"]["options_greeks"] == {
        "instrument_shape": "legs",
        "required_optional_blocks": ["legs", "greeks_exposure"],
        "minimum_legs": 1,
        "notes": [
            "declare_delta_gamma_vega_theta_limits",
            "validate_greeks_risk_control_before_paper",
        ],
    }
    assert payload["readiness_checks"] == [
        "choose_one_allowed_strategy_family",
        "keep_all_fixed_spec_fields_present",
        "use_legs_for_multi_leg_spread_straddle_strangle_calendar_or_greeks_shapes",
        "declare_greeks_exposure_for_options_greeks",
        "keep_validation_plan.net_edge_floor_pts_above_10",
        "do_not_change_cost_model_to_make_edge_pass",
    ]


def test_factory_parser_exposes_strategy_family_intake_template() -> None:
    parser = factory.build_parser()
    args = parser.parse_args(["strategy-family-intake-template", "--out", "intake.json"])

    assert args.func is factory.cmd_strategy_family_intake_template
    assert args.out == "intake.json"


def test_factory_strategy_family_intake_validate_marks_ready_for_spec(tmp_path: Path) -> None:
    spec = _valid_strategy_spec()
    spec["strategy_name"] = "txo_delta_neutral_v0"
    spec["instrument"] = ["TXO_C_23000_202606", "TXO_P_23000_202606"]
    intake = {
        "schema": "research.strategy_family_intake.request.v1",
        "strategy_family": "options_greeks",
        "spec": spec,
        "legs": [
            {"symbol": "TXO_C_23000_202606", "side": "long", "qty": 1},
            {"symbol": "TXO_P_23000_202606", "side": "short", "qty": 1},
        ],
        "greeks_exposure": {
            "max_net_delta": 0.2,
            "max_net_gamma": 0.05,
            "max_net_vega": 1.0,
            "max_net_theta": 1.0,
        },
    }
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps(intake), encoding="utf-8")
    out = tmp_path / "intake_validation.json"

    rc = factory.cmd_strategy_family_intake_validate(argparse.Namespace(intake=str(intake_path), out=str(out)))

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == "research.strategy_family_intake.validation.v1"
    assert payload["status"] == "ready_for_spec"
    assert payload["strategy_family"] == "options_greeks"
    assert payload["errors"] == []
    assert payload["missing_required_spec_fields"] == []
    assert payload["family_shape_requirement"] == {
        "instrument_shape": "legs",
        "required_optional_blocks": ["legs", "greeks_exposure"],
        "minimum_legs": 1,
        "notes": [
            "declare_delta_gamma_vega_theta_limits",
            "validate_greeks_risk_control_before_paper",
        ],
    }


def test_factory_strategy_family_intake_validate_blocks_bad_family_shape_and_floor(
    tmp_path: Path,
) -> None:
    spec = _valid_strategy_spec()
    spec.pop("exit_rule")
    spec["validation_plan"]["net_edge_floor_pts"] = 5.0
    intake_path = tmp_path / "bad_intake.json"
    intake_path.write_text(
        json.dumps(
            {
                "schema": "research.strategy_family_intake.request.v1",
                "strategy_family": "options_magic",
                "spec": spec,
                "greeks_exposure": {},
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "bad_intake_validation.json"

    rc = factory.cmd_strategy_family_intake_validate(argparse.Namespace(intake=str(intake_path), out=str(out)))

    assert rc == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "blocked"
    assert payload["errors"] == [
        "invalid_strategy_family",
        "missing_required_spec_fields",
        "net_edge_floor_below_10",
    ]
    assert payload["missing_required_spec_fields"] == ["exit_rule"]


def test_factory_strategy_family_intake_validate_blocks_missing_shape_blocks(
    tmp_path: Path,
) -> None:
    spec = _valid_strategy_spec()
    intake_path = tmp_path / "straddle_intake.json"
    intake_path.write_text(
        json.dumps(
            {
                "schema": "research.strategy_family_intake.request.v1",
                "strategy_family": "options_straddle",
                "spec": spec,
                "legs": [{"symbol": "TXO_C_23000_202606", "side": "long", "qty": 1}],
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "straddle_validation.json"

    rc = factory.cmd_strategy_family_intake_validate(argparse.Namespace(intake=str(intake_path), out=str(out)))

    assert rc == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "blocked"
    assert payload["errors"] == ["minimum_legs_not_met"]
    assert payload["shape_errors"] == ["minimum_legs_not_met"]


def test_factory_parser_exposes_strategy_family_intake_validate() -> None:
    parser = factory.build_parser()
    args = parser.parse_args(["strategy-family-intake-validate", "--intake", "intake.json", "--out", "validated.json"])

    assert args.func is factory.cmd_strategy_family_intake_validate
    assert args.intake == "intake.json"
    assert args.out == "validated.json"


def test_factory_strategy_family_intake_spec_plan_dry_run_projects_spec_without_writes(
    tmp_path: Path,
) -> None:
    spec = _valid_strategy_spec()
    spec["strategy_name"] = "txo_delta_neutral_v0"
    spec["instrument"] = ["TXO_C_23000_202606", "TXO_P_23000_202606"]
    intake = {
        "schema": "research.strategy_family_intake.request.v1",
        "strategy_family": "options_greeks",
        "spec": spec,
        "legs": [
            {"symbol": "TXO_C_23000_202606", "side": "long", "qty": 1},
            {"symbol": "TXO_P_23000_202606", "side": "short", "qty": 1},
        ],
        "greeks_exposure": {
            "max_net_delta": 0.2,
            "max_net_gamma": 0.05,
            "max_net_vega": 1.0,
            "max_net_theta": 1.0,
        },
    }
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps(intake), encoding="utf-8")
    validation_path = tmp_path / "validation.json"
    validation = factory._strategy_family_intake_validation_payload(intake_path, intake)
    validation_path.write_text(json.dumps(validation), encoding="utf-8")
    out = tmp_path / "spec_plan.json"

    rc = factory.cmd_strategy_family_intake_spec_plan(argparse.Namespace(validation=str(validation_path), out=str(out)))

    assert rc == 0
    assert not (tmp_path / "research" / "alphas" / "txo_delta_neutral_v0").exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == "research.strategy_family_intake.spec_plan.v1"
    assert payload["mode"] == "dry_run"
    assert payload["status"] == "ready_to_scaffold"
    assert payload["mutates_repo"] is False
    assert payload["candidate"] == "txo_delta_neutral_v0"
    assert payload["strategy_family"] == "options_greeks"
    assert payload["planned_paths"] == {
        "candidate_dir": "alphas/txo_delta_neutral_v0",
        "spec_path": "alphas/txo_delta_neutral_v0/spec.yaml",
    }
    assert payload["traceability_metadata"] == {
        "schema": "research.strategy_family_traceability.v1",
        "strategy_family": "options_greeks",
        "intake_path": str(intake_path.resolve()),
        "validation_path": str(validation_path.resolve()),
        "status": "ready_for_spec",
    }
    # legs / greeks_exposure are merged from the intake top level so the
    # scaffolded spec.yaml carries the full multi-leg / options definition.
    assert payload["spec"] == {
        **spec,
        "legs": intake["legs"],
        "greeks_exposure": intake["greeks_exposure"],
    }
    assert "strategy_name: txo_delta_neutral_v0" in payload["spec_yaml_preview"]
    assert "net_edge_floor_pts: 10.0" in payload["spec_yaml_preview"]
    assert payload["errors"] == []


def test_factory_strategy_family_intake_spec_plan_apply_writes_skeleton_when_ready(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    monkeypatch.setattr(factory, "ROOT", root)
    spec = _valid_strategy_spec()
    spec["strategy_name"] = "txo_delta_neutral_v0"
    spec["instrument"] = ["TXO_C_23000_202606", "TXO_P_23000_202606"]
    intake = {
        "schema": "research.strategy_family_intake.request.v1",
        "strategy_family": "options_greeks",
        "spec": spec,
        "legs": [
            {"symbol": "TXO_C_23000_202606", "side": "long", "qty": 1},
            {"symbol": "TXO_P_23000_202606", "side": "short", "qty": 1},
        ],
        "greeks_exposure": {
            "max_net_delta": 0.2,
            "max_net_gamma": 0.05,
            "max_net_vega": 1.0,
            "max_net_theta": 1.0,
        },
    }
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps(intake), encoding="utf-8")
    validation_path = tmp_path / "validation.json"
    validation = factory._strategy_family_intake_validation_payload(intake_path, intake)
    validation_path.write_text(json.dumps(validation), encoding="utf-8")
    out = tmp_path / "spec_plan_apply.json"

    rc = factory.cmd_strategy_family_intake_spec_plan(
        argparse.Namespace(validation=str(validation_path), out=str(out), apply=True)
    )

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "apply"
    assert payload["status"] == "applied"
    assert payload["mutates_repo"] is True
    assert payload["errors"] == []
    alpha_dir = root / "alphas" / "txo_delta_neutral_v0"
    assert (alpha_dir / "__init__.py").read_text(encoding="utf-8") == ""
    assert yaml.safe_load((alpha_dir / "spec.yaml").read_text(encoding="utf-8")) == {
        **spec,
        "legs": intake["legs"],
        "greeks_exposure": intake["greeks_exposure"],
    }
    traceability = json.loads((alpha_dir / "intake_traceability.json").read_text(encoding="utf-8"))
    assert traceability == {
        "schema": "research.strategy_family_traceability.v1",
        "strategy_family": "options_greeks",
        "intake_path": str(intake_path.resolve()),
        "validation_path": str(validation_path.resolve()),
        "status": "ready_for_spec",
    }


def test_factory_strategy_family_intake_spec_plan_apply_blocks_existing_candidate(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    monkeypatch.setattr(factory, "ROOT", root)
    spec = _valid_strategy_spec()
    spec["strategy_name"] = "txo_delta_neutral_v0"
    intake = {
        "schema": "research.strategy_family_intake.request.v1",
        "strategy_family": "futures_directional",
        "spec": spec,
        "legs": [{"symbol": "TXFD6", "side": "long", "qty": 1}],
    }
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(json.dumps(intake), encoding="utf-8")
    validation_path = tmp_path / "validation.json"
    validation = factory._strategy_family_intake_validation_payload(intake_path, intake)
    validation_path.write_text(json.dumps(validation), encoding="utf-8")
    alpha_dir = root / "alphas" / "txo_delta_neutral_v0"
    alpha_dir.mkdir(parents=True)
    existing_spec = alpha_dir / "spec.yaml"
    existing_spec.write_text("strategy_name: existing\n", encoding="utf-8")
    out = tmp_path / "blocked_apply.json"

    rc = factory.cmd_strategy_family_intake_spec_plan(
        argparse.Namespace(validation=str(validation_path), out=str(out), apply=True)
    )

    assert rc == 1
    assert existing_spec.read_text(encoding="utf-8") == "strategy_name: existing\n"
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "apply"
    assert payload["status"] == "blocked"
    assert payload["mutates_repo"] is False
    assert payload["errors"] == ["candidate_dir_exists", "spec_path_exists"]


def test_factory_strategy_family_intake_spec_plan_blocks_unsafe_candidate_name(
    tmp_path: Path,
) -> None:
    # A strategy_name encoding a path traversal must never become a scaffold path.
    spec = _valid_strategy_spec()
    spec["strategy_name"] = "../../escape"
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(
        json.dumps(
            {
                "schema": "research.strategy_family_intake.request.v1",
                "strategy_family": "futures_directional",
                "spec": spec,
            }
        ),
        encoding="utf-8",
    )
    # Hand a ready_for_spec validation so the guard under test (not the upstream
    # validation) is what blocks the plan.
    validation = {
        "schema": "research.strategy_family_intake.validation.v1",
        "intake_path": str(intake_path),
        "strategy_family": "futures_directional",
        "status": "ready_for_spec",
        "errors": [],
    }

    payload = factory._strategy_family_intake_spec_plan_payload(tmp_path / "v.json", validation)

    assert payload["status"] == "blocked"
    assert payload["mutates_repo"] is False
    assert payload["planned_paths"] == {}
    assert "strategy_name_unsafe" in payload["errors"]


def test_factory_strategy_family_intake_apply_blocks_paths_outside_alpha_root(monkeypatch, tmp_path: Path) -> None:
    # Defense in depth: even an injected planned path must not write outside
    # research/alphas.
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    monkeypatch.setattr(factory, "ROOT", root)
    payload = {
        "planned_paths": {
            "candidate_dir": "alphas/../../escape",
            "spec_path": "alphas/../../escape/spec.yaml",
        },
        "spec": {"strategy_name": "escape"},
        "traceability_metadata": {},
    }

    result = factory._apply_strategy_family_intake_spec_plan(payload)

    assert result["status"] == "blocked"
    assert result["mutates_repo"] is False
    assert "candidate_dir_outside_alpha_root" in result["errors"]
    assert not (tmp_path / "escape").exists()


def test_factory_strategy_family_intake_validate_blocks_invalid_spec_values(
    tmp_path: Path,
) -> None:
    # Required fields are present but a value is invalid (unsupported timeframe):
    # canonical validate_spec must block it here instead of passing as ready.
    spec = _valid_strategy_spec()
    spec["timeframe"] = "7m"
    intake_path = tmp_path / "intake.json"
    intake_path.write_text(
        json.dumps(
            {
                "schema": "research.strategy_family_intake.request.v1",
                "strategy_family": "futures_directional",
                "spec": spec,
                "legs": [{"symbol": "TXFD6", "side": "long", "qty": 1}],
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "validation.json"

    rc = factory.cmd_strategy_family_intake_validate(argparse.Namespace(intake=str(intake_path), out=str(out)))

    assert rc == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "blocked"
    assert "spec_invalid_values" in payload["errors"]
    assert any("timeframe=" in err for err in payload["spec_validation_errors"])


def test_research_candidate_blockers_fail_closed_on_missing_drawdown_gate() -> None:
    common = dict(
        full_edge=18.0,
        oos_edge=12.5,
        edge_floor=10.0,
        full_events=360,
        min_round_trips=300,
        oos_days=70,
        min_oos_days=60,
        oos_pnl_distribution_checked=True,
        oos_loss_distribution_checked=True,
        oos_single_trade_dominance_passed=True,
        oos_single_day_dominance_passed=True,
        decision_status="promising",
        promotion_blockers=[],
        parity_status="pass",
    )

    cleared = factory._research_candidate_blockers(drawdown_gate=True, **common)
    missing = factory._research_candidate_blockers(drawdown_gate=None, **common)
    failed = factory._research_candidate_blockers(drawdown_gate=False, **common)

    assert cleared == []
    assert "drawdown_gate_failed" in missing  # missing evidence is fail-closed
    assert "drawdown_gate_failed" in failed


def test_factory_strategy_family_intake_spec_plan_blocks_invalid_validation(
    tmp_path: Path,
) -> None:
    intake_path = tmp_path / "bad_intake.json"
    intake_path.write_text(
        json.dumps({"schema": "research.strategy_family_intake.request.v1", "strategy_family": "bad"}),
        encoding="utf-8",
    )
    validation_path = tmp_path / "bad_validation.json"
    validation_path.write_text(
        json.dumps(
            {
                "schema": "research.strategy_family_intake.validation.v1",
                "intake_path": str(intake_path.resolve()),
                "strategy_family": "bad",
                "status": "blocked",
                "errors": ["invalid_strategy_family"],
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "blocked_plan.json"

    rc = factory.cmd_strategy_family_intake_spec_plan(argparse.Namespace(validation=str(validation_path), out=str(out)))

    assert rc == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "blocked"
    assert payload["mutates_repo"] is False
    assert payload["errors"] == ["validation_not_ready_for_spec"]
    assert payload["planned_paths"] == {}
    assert payload["spec"] == {}


def test_factory_parser_exposes_strategy_family_intake_spec_plan() -> None:
    parser = factory.build_parser()
    args = parser.parse_args(
        ["strategy-family-intake-spec-plan", "--validation", "validated.json", "--out", "plan.json"]
    )

    assert args.func is factory.cmd_strategy_family_intake_spec_plan
    assert args.validation == "validated.json"
    assert args.out == "plan.json"
    assert args.apply is False

    apply_args = parser.parse_args(
        [
            "strategy-family-intake-spec-plan",
            "--validation",
            "validated.json",
            "--apply",
            "--out",
            "plan.json",
        ]
    )
    assert apply_args.apply is True


def test_factory_audit_reports_edge_runs_missing_metric_semantics(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    run_dir = root / "experiments" / "runs" / "legacy-edge-run"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": "legacy-edge-run",
                "alpha_id": "legacy_edge_alpha",
                "timestamp": "2026-06-02T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "scorecard.json").write_text(
        json.dumps({"sharpe_oos": 1.2}),
        encoding="utf-8",
    )
    (run_dir / "backtest_report.json").write_text(
        json.dumps(
            {
                "gate": "Gate C",
                "passed": True,
                "details": {
                    "sub_gates_advisory": [
                        {
                            "name": "edge_per_round_trip",
                            "passed": True,
                            "metrics": {"mean_net_edge_pts_per_trade": 12.5},
                            "details": "legacy edge gate",
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "audit_edge_semantics.json"
    rc = factory.cmd_audit(argparse.Namespace(out=str(out), fail_on_warning=False, data=[]))

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    edge_audit = payload["details"]["experiment_edge_metric_semantics"]
    assert edge_audit["reports_with_edge_gate"] == 1
    assert edge_audit["missing_semantics"] == [
        {
            "run_dir": "experiments/runs/legacy-edge-run",
            "report_path": "experiments/runs/legacy-edge-run/backtest_report.json",
            "scorecard_path": "experiments/runs/legacy-edge-run/scorecard.json",
            "missing": ["scorecard.edge_metric_semantics", "report.edge_metric_semantics"],
        }
    ]
    assert payload["warnings"] == []


def test_factory_audit_flags_edge_runs_with_unvalidated_semantics(monkeypatch, tmp_path: Path) -> None:
    from research.registry.schemas import edge_metric_semantics

    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    run_dir = root / "experiments" / "runs" / "unvalidated-edge-run"
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": "unvalidated-edge-run",
                "alpha_id": "edge_alpha",
                "timestamp": "2026-06-02T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    # Label present in both files, but inventory_mtm failed → not trustworthy.
    status = {
        "edge_per_round_trip": "pass",
        "inventory_mtm": "fail",
        "cost_uncertainty": "pass",
        "force_flat_residual": "pass",
        "min_sample_size": "pass",
        "single_day_dominance": "pass",
        "monthly_distribution": "pass",
    }
    semantics = edge_metric_semantics(supporting_gates_status=status, validated=False)
    (run_dir / "scorecard.json").write_text(
        json.dumps({"sharpe_oos": 1.2, "edge_metric_semantics": semantics}),
        encoding="utf-8",
    )
    (run_dir / "backtest_report.json").write_text(
        json.dumps(
            {
                "gate": "Gate C",
                "passed": False,
                "details": {
                    "edge_metric_semantics": semantics,
                    "sub_gates_advisory": [
                        {
                            "name": "edge_per_round_trip",
                            "passed": True,
                            "metrics": {"mean_net_edge_pts_per_trade": 12.5},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "audit_unvalidated.json"
    rc = factory.cmd_audit(argparse.Namespace(out=str(out), fail_on_warning=False, data=[]))

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    edge_audit = payload["details"]["experiment_edge_metric_semantics"]
    assert edge_audit["reports_with_edge_gate"] == 1
    assert edge_audit["missing_semantics"] == []
    assert edge_audit["complete"] == []
    assert edge_audit["unvalidated"] == [
        {
            "run_dir": "experiments/runs/unvalidated-edge-run",
            "report_path": "experiments/runs/unvalidated-edge-run/backtest_report.json",
            "scorecard_path": "experiments/runs/unvalidated-edge-run/scorecard.json",
            "failing_gates": ["inventory_mtm"],
        }
    ]


def test_factory_audit_reports_gate_c_runs_missing_research_decision(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")

    missing_dir = root / "experiments" / "runs" / "gate-c-missing-decision"
    missing_dir.mkdir(parents=True)
    (missing_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": "gate-c-missing-decision",
                "alpha_id": "decision_alpha",
                "timestamp": "2026-06-02T00:00:00+00:00",
                "gate_status": {"gate_c": False},
                "backtest_report_path": str(missing_dir / "backtest_report.json"),
            }
        ),
        encoding="utf-8",
    )
    (missing_dir / "backtest_report.json").write_text(
        json.dumps({"gate": "Gate C", "passed": False}),
        encoding="utf-8",
    )

    complete_dir = root / "experiments" / "runs" / "gate-c-with-decision"
    complete_dir.mkdir(parents=True)
    (complete_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": "gate-c-with-decision",
                "alpha_id": "decision_alpha",
                "timestamp": "2026-06-02T00:00:00+00:00",
                "gate_status": {"gate_c": False},
                "backtest_report_path": str(complete_dir / "backtest_report.json"),
                "research_decision": {
                    "status": "blocked_by_parity",
                    "reason": "gate_c_parity_blocker:replay_parity",
                    "evidence": ["replay_parity"],
                    "decided_by": "gate_c",
                },
            }
        ),
        encoding="utf-8",
    )
    (complete_dir / "backtest_report.json").write_text(
        json.dumps({"gate": "Gate C", "passed": False}),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "audit_research_decisions.json"
    rc = factory.cmd_audit(argparse.Namespace(out=str(out), fail_on_warning=False, data=[]))

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    decision_audit = payload["details"]["experiment_research_decisions"]
    assert decision_audit["scanned_meta"] == 2
    assert decision_audit["gate_c_runs"] == 2
    assert decision_audit["missing_decisions"] == [
        {
            "run_dir": "experiments/runs/gate-c-missing-decision",
            "meta_path": "experiments/runs/gate-c-missing-decision/meta.json",
            "report_path": "experiments/runs/gate-c-missing-decision/backtest_report.json",
            "missing": ["meta.research_decision"],
        }
    ]
    assert decision_audit["complete"] == [
        {
            "run_dir": "experiments/runs/gate-c-with-decision",
            "meta_path": "experiments/runs/gate-c-with-decision/meta.json",
            "report_path": "experiments/runs/gate-c-with-decision/backtest_report.json",
            "status": "blocked_by_parity",
            "reason": "gate_c_parity_blocker:replay_parity",
        }
    ]
    assert payload["warnings"] == []


def test_factory_audit_classifies_missing_research_decisions_by_derivability(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")

    derivable_dir = root / "experiments" / "runs" / "gate-c-derivable-decision"
    derivable_dir.mkdir(parents=True)
    (derivable_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": "gate-c-derivable-decision",
                "alpha_id": "decision_alpha",
                "timestamp": "2026-06-02T00:00:00+00:00",
                "gate_status": {"gate_c": False},
                "backtest_report_path": str(derivable_dir / "backtest_report.json"),
            }
        ),
        encoding="utf-8",
    )
    (derivable_dir / "backtest_report.json").write_text(
        json.dumps(
            {
                "gate": "Gate C",
                "passed": False,
                "details": {
                    "sub_gates_blocking": {
                        "passed": False,
                        "triage_status": "sample_needs_more_sample",
                        "triage_reasons": ["min_sample_size"],
                        "failing": [{"name": "min_sample_size", "passed": False, "metrics": {}}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    not_derivable_dir = root / "experiments" / "runs" / "gate-c-not-derivable-decision"
    not_derivable_dir.mkdir(parents=True)
    (not_derivable_dir / "meta.json").write_text(
        json.dumps(
            {
                "run_id": "gate-c-not-derivable-decision",
                "alpha_id": "decision_alpha",
                "timestamp": "2026-06-02T00:00:00+00:00",
                "gate_status": {"gate_c": False},
                "backtest_report_path": str(not_derivable_dir / "backtest_report.json"),
            }
        ),
        encoding="utf-8",
    )
    (not_derivable_dir / "backtest_report.json").write_text(
        json.dumps({"gate": "Gate C", "passed": False}),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "audit_research_decision_derivability.json"
    rc = factory.cmd_audit(argparse.Namespace(out=str(out), fail_on_warning=False, data=[]))

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    decision_audit = payload["details"]["experiment_research_decisions"]
    assert decision_audit["derivable_decisions"] == [
        {
            "run_dir": "experiments/runs/gate-c-derivable-decision",
            "meta_path": "experiments/runs/gate-c-derivable-decision/meta.json",
            "report_path": "experiments/runs/gate-c-derivable-decision/backtest_report.json",
            "research_decision": {
                "status": "needs_more_sample",
                "reason": "gate_c_sample_needs_more_sample",
                "evidence": ["min_sample_size"],
                "decided_by": "gate_c",
            },
        }
    ]
    assert decision_audit["not_derivable_decisions"] == [
        {
            "run_dir": "experiments/runs/gate-c-not-derivable-decision",
            "meta_path": "experiments/runs/gate-c-not-derivable-decision/meta.json",
            "report_path": "experiments/runs/gate-c-not-derivable-decision/backtest_report.json",
            "reason": "missing_gate_c_blocking_evidence",
        }
    ]
    assert payload["warnings"] == []


def test_factory_audit_indexes_latest_t1b_validation_summary(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    validation_dir = root / "experiments" / "validations" / "t1b_volcompress_v0"
    validation_dir.mkdir(parents=True)
    legacy_dir = root / "experiments" / "validations" / "legacy_untraceable_v0"
    legacy_dir.mkdir(parents=True)
    old_summary = {
        "candidate": "t1b_txf_volcompress_tmf",
        "artifact_scope": "validation_summary",
        "summary_path": str(validation_dir / "20260601T000000Z_summary.json"),
        "edge_floor_metric": "mean_net_edge_pts_per_trade",
        "research_decision": {
            "status": "needs_more_sample",
            "reason": "t1b_sample_gate:events",
            "evidence": ["min_sample_size", "events"],
            "decided_by": "t1b_v0_hard_gate",
        },
        "splits": {"full": {"mean_net_edge_pts_per_trade": 8.0}},
    }
    new_summary = {
        "candidate": "t1b_txf_volcompress_tmf",
        "artifact_scope": "validation_summary",
        "summary_path": str(validation_dir / "20260602T000000Z_summary.json"),
        "edge_floor_metric": "mean_net_edge_pts_per_trade",
        "edge_floor_cleared": True,
        "research_decision": {
            "status": "blocked_by_audit",
            "reason": "t1b_v0_audit_blocker:v0_latency_profile_deferred",
            "evidence": ["v0_latency_profile_deferred", "no_replay_paper_live_parity_evidence"],
            "decided_by": "t1b_v0_hard_gate",
        },
        "hard_gate": {"drawdown_within_2x_average_monthly_net_pnl": False},
        "splits": {
            "full": {
                "mean_net_edge_pts_per_trade": 12.5,
                "max_drawdown_net_pts": 31.0,
                "average_monthly_net_pnl": 11.0,
                "median_monthly_net_pnl": 9.0,
                "worst_month_net_pnl": -4.0,
                "max_single_month_net_share_of_positive": 0.64,
                "drawdown_within_2x_average_monthly_net_pnl": False,
            },
            "out_of_sample": {
                "mean_net_edge_pts_per_trade": 10.75,
                "max_drawdown_net_pts": 8.0,
                "average_monthly_net_pnl": 13.0,
                "median_monthly_net_pnl": 13.0,
                "worst_month_net_pnl": 13.0,
                "max_single_month_net_share_of_positive": 1.0,
                "drawdown_within_2x_average_monthly_net_pnl": True,
            },
        },
    }
    legacy_summary = {
        "candidate": "legacy_untraceable_candidate",
        "splits": {"full": {"events": 12}},
    }
    (validation_dir / "20260601T000000Z_summary.json").write_text(json.dumps(old_summary), encoding="utf-8")
    (validation_dir / "20260602T000000Z_summary.json").write_text(json.dumps(new_summary), encoding="utf-8")
    (legacy_dir / "20260602T000000Z_summary.json").write_text(json.dumps(legacy_summary), encoding="utf-8")

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "audit_validation_summaries.json"
    rc = factory.cmd_audit(argparse.Namespace(out=str(out), fail_on_warning=False, data=[]))

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    summary_index = payload["details"]["validation_summary_index"]
    assert summary_index["t1b_txf_volcompress_tmf"] == {
        "summary_path": "experiments/validations/t1b_volcompress_v0/20260602T000000Z_summary.json",
        "research_decision_status": "blocked_by_audit",
        "research_decision_reason": "t1b_v0_audit_blocker:v0_latency_profile_deferred",
        "research_decision_evidence": [
            "v0_latency_profile_deferred",
            "no_replay_paper_live_parity_evidence",
        ],
        "edge_floor_metric": "mean_net_edge_pts_per_trade",
        "mean_net_edge_pts_per_trade": 12.5,
        "edge_floor_cleared": True,
        "risk_gate_drawdown_within_2x_average_monthly_net_pnl": False,
        "full_max_drawdown_net_pts": 31.0,
        "full_average_monthly_net_pnl": 11.0,
        "full_median_monthly_net_pnl": 9.0,
        "full_worst_month_net_pnl": -4.0,
        "full_max_single_month_net_share_of_positive": 0.64,
        "full_drawdown_within_2x_average_monthly_net_pnl": False,
        "out_of_sample_mean_net_edge_pts_per_trade": 10.75,
        "out_of_sample_max_drawdown_net_pts": 8.0,
        "out_of_sample_average_monthly_net_pnl": 13.0,
        "out_of_sample_median_monthly_net_pnl": 13.0,
        "out_of_sample_worst_month_net_pnl": 13.0,
        "out_of_sample_max_single_month_net_share_of_positive": 1.0,
        "out_of_sample_drawdown_within_2x_average_monthly_net_pnl": True,
        "traceability_missing": [],
    }
    assert payload["details"]["research_decision_replay"] == [
        {
            "candidate": "legacy_untraceable_candidate",
            "replay_status": "legacy_untraceable",
            "status": "",
            "reason": "",
            "summary_path": "experiments/validations/legacy_untraceable_v0/20260602T000000Z_summary.json",
            "spec_path": "",
            "readiness_status": "legacy_untraceable",
            "paper_live_eligible": False,
            "primary_blocker": "legacy_traceability_missing",
            "blockers": ["legacy_traceability_missing"],
            "next_actions": [
                "backfill_validation_summary_identity_fields",
                "backfill_research_decision_status_reason_evidence",
                "backfill_round_trip_net_edge_metrics",
                "exclude_from_paper_live_candidate_comparison_until_backfilled",
            ],
            "command_families": [],
            "edge_floor_metric": "",
            "mean_net_edge_pts_per_trade": None,
            "edge_floor_cleared": False,
            "out_of_sample_mean_net_edge_pts_per_trade": None,
            "risk_gate_drawdown_within_2x_average_monthly_net_pnl": None,
            "full_max_drawdown_net_pts": None,
            "full_average_monthly_net_pnl": None,
            "full_worst_month_net_pnl": None,
            "traceability_missing": [
                "artifact_scope",
                "summary_path",
                "research_decision",
                "edge_floor_metric",
                "splits.full.mean_net_edge_pts_per_trade",
            ],
        },
        {
            "candidate": "t1b_txf_volcompress_tmf",
            "replay_status": "traceable",
            "status": "blocked_by_audit",
            "reason": "t1b_v0_audit_blocker:v0_latency_profile_deferred",
            "summary_path": "experiments/validations/t1b_volcompress_v0/20260602T000000Z_summary.json",
            "spec_path": "",
            "readiness_status": "",
            "paper_live_eligible": False,
            "primary_blocker": "",
            "blockers": [],
            "next_actions": [],
            "command_families": [],
            "edge_floor_metric": "mean_net_edge_pts_per_trade",
            "mean_net_edge_pts_per_trade": 12.5,
            "edge_floor_cleared": True,
            "out_of_sample_mean_net_edge_pts_per_trade": 10.75,
            "risk_gate_drawdown_within_2x_average_monthly_net_pnl": False,
            "full_max_drawdown_net_pts": 31.0,
            "full_average_monthly_net_pnl": 11.0,
            "full_worst_month_net_pnl": -4.0,
            "traceability_missing": [],
        },
    ]


def test_factory_backfill_research_decisions_dry_run_writes_plan_without_mutating_meta(
    monkeypatch, tmp_path: Path
) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")

    run_dir = root / "experiments" / "runs" / "gate-c-derivable-decision"
    run_dir.mkdir(parents=True)
    meta_payload = {
        "run_id": "gate-c-derivable-decision",
        "alpha_id": "decision_alpha",
        "timestamp": "2026-06-02T00:00:00+00:00",
        "gate_status": {"gate_c": False},
        "backtest_report_path": str(run_dir / "backtest_report.json"),
    }
    meta_path = run_dir / "meta.json"
    meta_path.write_text(json.dumps(meta_payload, indent=2, sort_keys=True), encoding="utf-8")
    (run_dir / "backtest_report.json").write_text(
        json.dumps(
            {
                "gate": "Gate C",
                "passed": False,
                "details": {
                    "sub_gates_blocking": {
                        "passed": False,
                        "triage_status": "sample_needs_more_sample",
                        "triage_reasons": ["min_sample_size"],
                        "failing": [{"name": "min_sample_size", "passed": False, "metrics": {}}],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    out = tmp_path / "backfill_plan.json"
    rc = factory.cmd_backfill_research_decisions(argparse.Namespace(out=str(out), apply=False))

    assert rc == 0
    assert json.loads(meta_path.read_text(encoding="utf-8")) == meta_payload

    plan = json.loads(out.read_text(encoding="utf-8"))
    assert plan["mode"] == "dry_run"
    assert plan["apply"] is False
    assert plan["planned_count"] == 1
    assert plan["skipped_count"] == 0
    assert plan["planned"] == [
        {
            "run_dir": "experiments/runs/gate-c-derivable-decision",
            "meta_path": "experiments/runs/gate-c-derivable-decision/meta.json",
            "report_path": "experiments/runs/gate-c-derivable-decision/backtest_report.json",
            "research_decision": {
                "status": "needs_more_sample",
                "reason": "gate_c_sample_needs_more_sample",
                "evidence": ["min_sample_size"],
                "decided_by": "gate_c",
            },
        }
    ]


def test_factory_parser_exposes_research_decision_backfill_dry_run() -> None:
    parser = factory.build_parser()
    args = parser.parse_args(["backfill-research-decisions", "--out", "plan.json"])

    assert args.func is factory.cmd_backfill_research_decisions
    assert args.out == "plan.json"
    assert args.apply is False


# ---------------------------------------------------------------------------
# P0: AlphaManifest skills/roles fields + factory audit warning
# ---------------------------------------------------------------------------


def test_alpha_manifest_roles_skills_default_empty() -> None:
    """AlphaManifest defaults roles_used and skills_used to empty tuple."""
    from research.registry.schemas import AlphaManifest

    m = AlphaManifest(
        alpha_id="test_alpha",
        hypothesis="h",
        formula="f",
        paper_refs=("122",),
        data_fields=("ofi_l1",),
        complexity="O(1)",
    )
    assert m.roles_used == ()
    assert m.skills_used == ()


def test_alpha_manifest_roles_skills_roundtrip() -> None:
    """AlphaManifest roles_used/skills_used survive to_dict/from_dict round-trip."""
    from research.registry.schemas import AlphaManifest

    m = AlphaManifest(
        alpha_id="test_alpha",
        hypothesis="h",
        formula="f",
        paper_refs=(),
        data_fields=(),
        complexity="O(1)",
        roles_used=("planner", "code-reviewer"),
        skills_used=("iterative-retrieval", "hft-backtester"),
    )
    data = m.to_dict()
    assert list(data["roles_used"]) == ["planner", "code-reviewer"]
    assert list(data["skills_used"]) == ["iterative-retrieval", "hft-backtester"]

    m2 = AlphaManifest.from_dict(data)
    assert m2.roles_used == ("planner", "code-reviewer")
    assert m2.skills_used == ("iterative-retrieval", "hft-backtester")


def test_factory_audit_warns_when_alpha_has_no_skills(monkeypatch, tmp_path: Path) -> None:
    """Factory audit warns when a governed alpha's manifest has empty skills_used."""
    import research.factory as fct
    from research.registry.schemas import AlphaManifest

    root = tmp_path / "research"
    _bootstrap_research_root(root)

    # Build a minimal governed alpha structure (file layout)
    alpha_dir = root / "alphas" / "dummy_alpha"
    tests_dir = alpha_dir / "tests"
    tests_dir.mkdir(parents=True)
    (alpha_dir / "__init__.py").write_text("")
    (alpha_dir / "README.md").write_text("# dummy\n")
    (alpha_dir / "impl.py").write_text("")
    (tests_dir / "test_dummy.py").write_text("def test_placeholder(): pass\n")

    # Stub AlphaRegistry.discover to return a controlled alpha with empty skills_used
    class _DummyAlpha:
        @property
        def manifest(self):
            return AlphaManifest(
                alpha_id="dummy_alpha",
                hypothesis="h",
                formula="f",
                paper_refs=(),
                data_fields=(),
                complexity="O(1)",
                # skills_used defaults to () — triggers factory warning
            )

        def update(self, *a, **k):
            return 0.0

        def reset(self):
            pass

        def get_signal(self):
            return 0.0

    from research.registry import alpha_registry as _ar_mod

    class _StubRegistry:
        errors = ()

        def discover(self, _path):
            return {"dummy_alpha": _DummyAlpha()}

    monkeypatch.setattr(_ar_mod, "AlphaRegistry", _StubRegistry)
    monkeypatch.setattr(fct, "ROOT", root)
    out = tmp_path / "audit_skills.json"
    rc = fct.cmd_audit(argparse.Namespace(out=str(out), fail_on_warning=False, data=[]))
    payload = json.loads(out.read_text(encoding="utf-8"))
    contract = payload["details"]["alpha_contract"]
    assert "dummy_alpha" in contract["alphas_missing_skills"]
    # rc=0 because fail_on_warning is False
    assert rc == 0
    # confirm warning message present
    assert any("skills_used" in w for w in payload["warnings"])


def test_factory_audit_allows_canonical_templates_root(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "templates").mkdir()
    (root / "templates" / "strategy_spec.yaml").write_text("strategy_name: demo\n", encoding="utf-8")

    monkeypatch.setattr(factory, "ROOT", root)
    errors: list[str] = []
    details: dict = {}
    factory._audit_root_layout(errors, details)

    assert "templates" not in details["unexpected_root_dirs"]
    assert not errors


def test_factory_audit_treats_lifecycle_audit_as_core_tool(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "tools" / "lifecycle_audit.py").write_text("def main(): pass\n", encoding="utf-8")

    monkeypatch.setattr(factory, "ROOT", root)
    errors: list[str] = []
    details: dict = {}
    factory._audit_tools_layout(errors, details)

    assert "tools/lifecycle_audit.py" not in details["tools_layout"]["unexpected_root_scripts"]
    assert not errors


def test_paper_ref_audit_reads_manifests_without_importing_impls(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text(
        json.dumps({"known_ref": {"title": "known"}}),
        encoding="utf-8",
    )
    alpha_dir = root / "alphas" / "import_broken"
    alpha_dir.mkdir()
    (alpha_dir / "impl.py").write_text("import definitely_missing_module\n", encoding="utf-8")
    (alpha_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "alpha_id: import_broken",
                "status: prototype",
                "hypothesis: h",
                "formula: f",
                "paper_refs:",
                "  - known_ref",
                "data_fields: []",
                "complexity: O(1)",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    warnings: list[str] = []
    details: dict = {}
    factory._audit_paper_refs(warnings, details)

    assert warnings == []
    assert details["unresolved_paper_refs"] == {}


def test_paper_ref_audit_accepts_index_aliases_and_local_alpha_refs(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text(
        json.dumps(
            {
                "133": {
                    "ref": "133",
                    "arxiv_id": "2409.12721",
                    "title": "Market Simulation under Adverse Selection",
                }
            }
        ),
        encoding="utf-8",
    )
    parent_dir = root / "alphas" / "parent_alpha"
    parent_dir.mkdir()
    (parent_dir / "manifest.yaml").write_text("alpha_id: parent_alpha\n", encoding="utf-8")
    child_dir = root / "alphas" / "child_alpha"
    child_dir.mkdir()
    (child_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "alpha_id: child_alpha",
                "paper_refs:",
                "  - 2409.12721v2 Lalor & Swishchuk (2024) Market Simulation under Adverse Selection",
                "  - parent_alpha",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    warnings: list[str] = []
    details: dict = {}
    factory._audit_paper_refs(warnings, details)

    assert warnings == []
    assert details["unresolved_paper_refs"] == {}


def test_paper_ref_audit_accepts_explicit_paper_index_aliases(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text(
        json.dumps(
            {
                "122": {
                    "ref": "122",
                    "arxiv_id": "1011.6402v3",
                    "title": "The Price Impact of Order Book Events",
                    "aliases": ["Cont-Kukanov 2014 OFI"],
                }
            }
        ),
        encoding="utf-8",
    )
    alpha_dir = root / "alphas" / "ofi_taker"
    alpha_dir.mkdir()
    (alpha_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "alpha_id: ofi_taker",
                "paper_refs:",
                "  - Cont-Kukanov 2014 OFI",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    warnings: list[str] = []
    details: dict = {}
    factory._audit_paper_refs(warnings, details)

    assert details["unresolved_paper_refs"] == {}
    assert warnings == []


def test_paper_ref_audit_accepts_existing_local_artifact_aliases(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    r47_dir = root / "alphas" / "r47_maker_pivot"
    r47_dir.mkdir()
    (r47_dir / "manifest.yaml").write_text("alpha_id: r47_maker_pivot\n", encoding="utf-8")
    audit_path = tmp_path / "docs" / "incidents" / "2026-04-24-r47-backtest-credibility-audit.md"
    audit_path.parent.mkdir(parents=True)
    audit_path.write_text("# R47 backtest credibility audit\n", encoding="utf-8")
    backtest_selection_path = tmp_path / "docs" / "runbooks" / "backtest-engine-selection.md"
    backtest_selection_path.parent.mkdir(parents=True)
    backtest_selection_path.write_text(
        "Bias matrix references backtest_method_reliability.md.\n",
        encoding="utf-8",
    )
    mm_skill_path = tmp_path / ".agent" / "skills" / "hft-mm-design" / "SKILL.md"
    mm_skill_path.parent.mkdir(parents=True)
    mm_skill_path.write_text("## Structural Properties\nR47 validated properties.\n", encoding="utf-8")
    economics_path = tmp_path / "outputs" / "team_artifacts" / "alpha-research" / "r47_tmfd6_economics.md"
    economics_path.parent.mkdir(parents=True)
    economics_path.write_text("# R47 TMFD6 economics\nCK-direct source table.\n", encoding="utf-8")
    child_dir = root / "alphas" / "child_alpha"
    child_dir.mkdir()
    (child_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "alpha_id: child_alpha",
                "paper_refs:",
                "  - r47_maker_strategy",
                "  - r47_backtest_data_regression",
                "  - r47_structural_properties",
                "  - memory/backtest_method_reliability",
                "  - r47_tmfd6_economics",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    warnings: list[str] = []
    details: dict = {}
    factory._audit_paper_refs(warnings, details)

    assert details["unresolved_paper_refs"] == {}
    assert details["resolved_local_research_refs"] == {
        "memory/backtest_method_reliability": "docs/runbooks/backtest-engine-selection.md",
        "r47_backtest_data_regression": "docs/incidents/2026-04-24-r47-backtest-credibility-audit.md",
        "r47_maker_strategy": "research/alphas/r47_maker_pivot/manifest.yaml",
        "r47_structural_properties": ".agent/skills/hft-mm-design/SKILL.md",
        "r47_tmfd6_economics": "outputs/team_artifacts/alpha-research/r47_tmfd6_economics.md",
    }
    assert warnings == []


def test_paper_ref_audit_accepts_prior_run_kill_artifact_alias(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    kill_artifact_path = (
        tmp_path
        / "outputs"
        / "team_artifacts"
        / "alpha-research"
        / "archive"
        / "halted-2026-04-18-pre-B-C"
        / "round-7"
        / "artifacts"
        / "t1_researcher_proposal.md"
    )
    kill_artifact_path.parent.mkdir(parents=True)
    kill_artifact_path.write_text(
        "# R7-T1 Researcher Proposal\nC13_vol_of_vol_percentile_meta_gate SELF-RECOMMENDED KILL.\n",
        encoding="utf-8",
    )
    alpha_dir = root / "alphas" / "vol_inversion"
    alpha_dir.mkdir()
    (alpha_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "alpha_id: vol_inversion",
                "paper_refs:",
                "  - c13_vol_gate_disable_R7_kill",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    warnings: list[str] = []
    details: dict = {}
    factory._audit_paper_refs(warnings, details)

    assert details["unresolved_paper_refs"] == {}
    assert details["resolved_local_research_refs"] == {
        "c13_vol_gate_disable_R7_kill": (
            "outputs/team_artifacts/alpha-research/archive/"
            "halted-2026-04-18-pre-B-C/round-7/artifacts/t1_researcher_proposal.md"
        )
    }
    assert warnings == []


def test_paper_ref_audit_accepts_archived_round_summary_alias(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    summary_path = (
        tmp_path
        / "outputs"
        / "team_artifacts"
        / "alpha-research"
        / "archive"
        / "halted-2026-04-19-inst-options"
        / "round-7"
        / "summary.md"
    )
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(
        "# R7 Summary - C66 TXF-TMF Passive Pair MM\n"
        "Scenario B' realistic 20 TMF maker + 1 TXF take-hedge = -940 NTD.\n",
        encoding="utf-8",
    )
    alpha_dir = root / "alphas" / "basis_mean_reversion"
    alpha_dir.mkdir()
    (alpha_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "alpha_id: basis_mean_reversion",
                "paper_refs:",
                "  - r7_summary C66 hedge-cost-dominance lesson",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    warnings: list[str] = []
    details: dict = {}
    factory._audit_paper_refs(warnings, details)

    assert details["unresolved_paper_refs"] == {}
    assert details["resolved_local_research_refs"] == {
        "r7_summary C66 hedge-cost-dominance lesson": (
            "outputs/team_artifacts/alpha-research/archive/halted-2026-04-19-inst-options/round-7/summary.md"
        )
    }
    assert warnings == []


def test_paper_ref_audit_accepts_amhp_user_research_alias(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    t1_artifact_path = (
        tmp_path / "docs" / "alpha-research" / "round-1-hawkes-amhp" / "artifacts" / "t1_researcher_c1.md"
    )
    t1_artifact_path.parent.mkdir(parents=True)
    t1_artifact_path.write_text(
        "# T1 Researcher Report\nThe AMHP source is user-supplied: 六、AMHP + 七、應用場景.\n",
        encoding="utf-8",
    )
    alpha_dir = root / "alphas" / "r52_amhp_dynamic_spread"
    alpha_dir.mkdir()
    (alpha_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "alpha_id: r52_amhp_dynamic_spread",
                "paper_refs:",
                "  - AMHP-2024",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    warnings: list[str] = []
    details: dict = {}
    factory._audit_paper_refs(warnings, details)

    assert details["unresolved_paper_refs"] == {}
    assert details["resolved_local_research_refs"] == {
        "AMHP-2024": "docs/alpha-research/round-1-hawkes-amhp/artifacts/t1_researcher_c1.md"
    }
    assert warnings == []


def test_paper_ref_audit_classifies_unresolved_refs_for_repair(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text(
        json.dumps({"known_ref": {"title": "known"}}),
        encoding="utf-8",
    )
    alpha_dir = root / "alphas" / "needs_repair"
    alpha_dir.mkdir()
    (alpha_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "alpha_id: needs_repair",
                "paper_refs:",
                "  - memory/backtest_method_reliability",
                "  - 2403.02572v4 Lokin-Yu fill probability",
                "  - 2008 Avellaneda-Stoikov HFT in LOB",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    warnings: list[str] = []
    details: dict = {}
    factory._audit_paper_refs(warnings, details)

    assert details["unresolved_paper_refs"] == {
        "needs_repair": [
            "memory/backtest_method_reliability",
            "2403.02572v4 Lokin-Yu fill probability",
            "2008 Avellaneda-Stoikov HFT in LOB",
        ]
    }
    assert details["unresolved_paper_ref_classes"] == {
        "needs_repair": [
            {"ref": "memory/backtest_method_reliability", "reason": "local_research_ref_not_indexed"},
            {"ref": "2403.02572v4 Lokin-Yu fill probability", "reason": "arxiv_ref_not_indexed"},
            {"ref": "2008 Avellaneda-Stoikov HFT in LOB", "reason": "external_citation_not_indexed"},
        ]
    }
    assert warnings == ["Some manifest paper_refs are not mapped in research/knowledge/paper_index.json."]


def test_paper_ref_audit_exposes_fee_structure_repair_hint(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    cost_profiles = tmp_path / "config" / "research" / "cost_profiles.yaml"
    cost_profiles.parent.mkdir(parents=True)
    cost_profiles.write_text("TMFD6:\n  commission_pts_per_side: 1.3\n", encoding="utf-8")
    researcher_role = tmp_path / ".agent" / "teams" / "alpha-research" / "roles" / "researcher.md"
    researcher_role.parent.mkdir(parents=True)
    researcher_role.write_text("Cost-Source Gate: TXF ~3 pt, TMF ~4 pt.\n", encoding="utf-8")
    da_role = researcher_role.parent / "devils-advocate.md"
    da_role.write_text("Verify RT base against memory/feedback_taifex_fee_structure.md.\n", encoding="utf-8")
    c1_manifest = root / "alphas" / "c1_revalidation_txfd6_chavez_casillas_adaptive" / "manifest.yaml"
    c1_manifest.parent.mkdir()
    c1_manifest.write_text("alpha_id: c1\ncost_profile_notes:\n  source_memo: memory/feedback\n", encoding="utf-8")
    c30_manifest = root / "alphas" / "c30_txf_maker_tmf_hedge_pair" / "manifest.yaml"
    c30_manifest.parent.mkdir()
    c30_manifest.write_text("alpha_id: c30\ncost_profile_notes:\n  source_memo: memory/feedback\n", encoding="utf-8")
    alpha_dir = root / "alphas" / "fee_repair"
    alpha_dir.mkdir()
    (alpha_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "alpha_id: fee_repair",
                "paper_refs:",
                "  - feedback_taifex_fee_structure",
                "  - r47_tmfd6_economics",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    warnings: list[str] = []
    details: dict = {}
    factory._audit_paper_refs(warnings, details)

    assert details["unresolved_paper_refs"] == {"fee_repair": ["feedback_taifex_fee_structure", "r47_tmfd6_economics"]}
    assert details["local_research_ref_repair_hints"] == {
        "feedback_taifex_fee_structure": {
            "missing_path": "memory/feedback_taifex_fee_structure.md",
            "candidate_paths": [
                "config/research/cost_profiles.yaml",
                ".agent/teams/alpha-research/roles/researcher.md",
                ".agent/teams/alpha-research/roles/devils-advocate.md",
                "research/alphas/c1_revalidation_txfd6_chavez_casillas_adaptive/manifest.yaml",
                "research/alphas/c30_txf_maker_tmf_hedge_pair/manifest.yaml",
            ],
            "repair_action": (
                "Restore the missing memory file or promote one current cost-source gate artifact "
                "before resolving this cost-related reference."
            ),
        }
    }
    assert warnings == ["Some manifest paper_refs are not mapped in research/knowledge/paper_index.json."]


def test_paper_ref_audit_exposes_shared_context_cost_model_repair_hint(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    (root / "knowledge").mkdir()
    (root / "knowledge" / "paper_index.json").write_text("{}", encoding="utf-8")
    cost_profiles = tmp_path / "config" / "research" / "cost_profiles.yaml"
    cost_profiles.parent.mkdir(parents=True)
    cost_profiles.write_text("TMFD6:\n  commission_pts_per_side: 1.3\n", encoding="utf-8")
    run_archive = (
        tmp_path / "outputs" / "team_artifacts" / "alpha-research" / "archive" / "halted-2026-04-19-inst-options"
    )
    run_archive.mkdir(parents=True)
    (run_archive / "candidate_pool.json").write_text(
        '{"preconditions":["B_institutional_fee_tier_estimate"]}\n',
        encoding="utf-8",
    )
    (run_archive / "progress.jsonl").write_text(
        '{"event":"stage_complete","cost_drag_pct":20}\n',
        encoding="utf-8",
    )
    alpha_dir = root / "alphas" / "cost_model_repair"
    alpha_dir.mkdir()
    (alpha_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "alpha_id: cost_model_repair",
                "paper_refs:",
                "  - shared-context_2026-04-19_cost_model",
                "  - r47_tmfd6_economics",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(factory, "ROOT", root)
    warnings: list[str] = []
    details: dict = {}
    factory._audit_paper_refs(warnings, details)

    assert details["unresolved_paper_refs"] == {
        "cost_model_repair": ["shared-context_2026-04-19_cost_model", "r47_tmfd6_economics"]
    }
    assert details["local_research_ref_repair_hints"] == {
        "shared-context_2026-04-19_cost_model": {
            "missing_path": "shared-context_2026-04-19_cost_model",
            "candidate_paths": [
                "outputs/team_artifacts/alpha-research/archive/halted-2026-04-19-inst-options/candidate_pool.json",
                "outputs/team_artifacts/alpha-research/archive/halted-2026-04-19-inst-options/progress.jsonl",
                "config/research/cost_profiles.yaml",
            ],
            "repair_action": (
                "Recover the 2026-04-19 shared-context cost-model snapshot or promote a dated "
                "cost-model provenance note before resolving this institutional-estimate reference."
            ),
        }
    }
    assert warnings == ["Some manifest paper_refs are not mapped in research/knowledge/paper_index.json."]


def test_factory_audit_resolves_current_manifest_paper_refs() -> None:
    warnings: list[str] = []
    details: dict = {}

    factory._audit_paper_refs(warnings, details)

    assert details["unresolved_paper_refs"] == {}
    assert details["unresolved_paper_ref_classes"] == {}
    assert warnings == []


def test_paper_index_covers_manifest_arxiv_refs() -> None:
    payload = json.loads((factory.ROOT / "knowledge" / "paper_index.json").read_text(encoding="utf-8"))
    aliases = factory._paper_index_aliases(payload)

    required_arxiv_ids = {
        "1105.3115",
        "1206.4810",
        "1312.0514",
        "1806.05101",
        "1806.05849",
        "1812.07369",
        "1903.07222",
        "2211.00496",
        "2403.02572",
        "2405.11444",
        "2502.18625",
        "2508.16588",
        "2510.27334",
    }

    assert required_arxiv_ids <= aliases


def test_paper_index_covers_foundational_market_making_aliases() -> None:
    payload = json.loads((factory.ROOT / "knowledge" / "paper_index.json").read_text(encoding="utf-8"))
    aliases = factory._paper_index_aliases(payload)

    assert {
        "2008 Avellaneda-Stoikov",
        "2008 Avellaneda-Stoikov HFT in LOB",
    } <= aliases


def test_paper_index_covers_algorithmic_hft_book_aliases() -> None:
    payload = json.loads((factory.ROOT / "knowledge" / "paper_index.json").read_text(encoding="utf-8"))
    aliases = factory._paper_index_aliases(payload)

    assert {
        "2015 Cartea-Jaimungal Optimal execution with limit and market orders",
        "2015 Cartea-Jaimungal-Penalva",
        "2015 Cartea-Jaimungal-Penalva MM economics",
    } <= aliases


def test_paper_index_covers_queue_dynamics_aliases() -> None:
    payload = json.loads((factory.ROOT / "knowledge" / "paper_index.json").read_text(encoding="utf-8"))
    aliases = factory._paper_index_aliases(payload)

    assert {"2010 Cont-Stoikov-Talreja queue fill probability"} <= aliases


def test_paper_index_covers_microprice_aliases() -> None:
    payload = json.loads((factory.ROOT / "knowledge" / "paper_index.json").read_text(encoding="utf-8"))
    aliases = factory._paper_index_aliases(payload)

    assert {
        "2014_Stoikov_microprice",
        "2018 Stoikov micro-price",
    } <= aliases


def test_paper_index_accepts_queue_position_valuation_aliases() -> None:
    payload = json.loads((factory.ROOT / "knowledge" / "paper_index.json").read_text(encoding="utf-8"))
    aliases = factory._paper_index_aliases(payload)

    assert {
        "2017 Moallemi-Yuan Queue value on LOB",
        "2017_Moallemi_Yuan_queue_value_LOB",
    } <= aliases


def test_paper_index_accepts_fragmented_lob_queueing_aliases() -> None:
    payload = json.loads((factory.ROOT / "knowledge" / "paper_index.json").read_text(encoding="utf-8"))
    aliases = factory._paper_index_aliases(payload)

    assert {
        "2014 Maglaras multi-class LOB with heterogeneous agents",
        "2014_Maglaras_multi_class_LOB",
    } <= aliases


def test_paper_index_accepts_paris_bourse_lob_aliases() -> None:
    payload = json.loads((factory.ROOT / "knowledge" / "paper_index.json").read_text(encoding="utf-8"))
    aliases = factory._paper_index_aliases(payload)

    assert {
        "1995 Biais-Hillion-Spatt LOB",
        "1995_Biais_Hillion_Spatt_LOB",
    } <= aliases


def test_paper_index_accepts_order_book_liquidation_aliases() -> None:
    payload = json.loads((factory.ROOT / "knowledge" / "paper_index.json").read_text(encoding="utf-8"))
    aliases = factory._paper_index_aliases(payload)

    assert {
        "2014 Stoikov-Waeber Optimal asset liquidation",
        "2014_Stoikov_Waeber_optimal_liquidation",
    } <= aliases


def test_paper_index_accepts_futures_basis_microstructure_aliases() -> None:
    payload = json.loads((factory.ROOT / "knowledge" / "paper_index.json").read_text(encoding="utf-8"))
    aliases = factory._paper_index_aliases(payload)

    assert {
        "Fanelli 2023 futures-basis microstructure (cited in DA T2)",
        "Fanelli_2023_futures_basis_microstructure",
        "2309.00875",
    } <= aliases


def test_binary_pollution_allows_committed_q_hat_fixtures_only(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "research"
    _bootstrap_research_root(root)
    q_hat_dir = root / "backtest" / "q_hat_data"
    q_hat_dir.mkdir(parents=True)
    (q_hat_dir / "tmfd6_q_hat.parquet").write_bytes(b"fixture")
    (root / "backtest" / "scratch.parquet").write_bytes(b"bad")

    monkeypatch.setattr(factory, "ROOT", root)
    warnings: list[str] = []
    details: dict = {}
    factory._audit_binary_pollution(warnings, details)

    assert details["binary_pollution_in_source_zones"] == ["backtest/scratch.parquet"]
    assert warnings == ["Binary artifacts detected in source zones; move to research/data or research/archive."]


# ---------------------------------------------------------------------------
# P4: feature_set_version in AlphaManifest + from_dict round-trip
# ---------------------------------------------------------------------------


def test_alpha_manifest_feature_set_version_default_none() -> None:
    """AlphaManifest.feature_set_version defaults to None."""
    from research.registry.schemas import AlphaManifest

    m = AlphaManifest(
        alpha_id="test",
        hypothesis="h",
        formula="f",
        paper_refs=(),
        data_fields=(),
        complexity="O(1)",
    )
    assert m.feature_set_version is None


def test_alpha_manifest_feature_set_version_roundtrip() -> None:
    """feature_set_version survives to_dict/from_dict round-trip."""
    from research.registry.schemas import AlphaManifest

    m = AlphaManifest(
        alpha_id="test",
        hypothesis="h",
        formula="f",
        paper_refs=(),
        data_fields=(),
        complexity="O(1)",
        feature_set_version="lob_shared_v1",
    )
    data = m.to_dict()
    assert data["feature_set_version"] == "lob_shared_v1"
    m2 = AlphaManifest.from_dict(data)
    assert m2.feature_set_version == "lob_shared_v1"


def test_feature_set_version_constant_matches_default_set() -> None:
    """FEATURE_SET_VERSION constant equals the default FeatureSet id."""
    from hft_platform.feature.registry import FEATURE_SET_VERSION, build_default_lob_feature_set_v3

    fs = build_default_lob_feature_set_v3()
    assert fs.feature_set_id == FEATURE_SET_VERSION
