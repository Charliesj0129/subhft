from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

import research.factory as factory
import research.tools.data_governance as data_governance


def _bootstrap_research_root(root: Path) -> None:
    (root / "alphas").mkdir(parents=True, exist_ok=True)
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "data" / "raw").mkdir(parents=True, exist_ok=True)
    (root / "data" / "interim").mkdir(parents=True, exist_ok=True)
    (root / "data" / "processed").mkdir(parents=True, exist_ok=True)


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
        tmp_path
        / "docs"
        / "alpha-research"
        / "round-1-hawkes-amhp"
        / "artifacts"
        / "t1_researcher_c1.md"
    )
    t1_artifact_path.parent.mkdir(parents=True)
    t1_artifact_path.write_text(
        "# T1 Researcher Report\n"
        "The AMHP source is user-supplied: 六、AMHP + 七、應用場景.\n",
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
        tmp_path
        / "outputs"
        / "team_artifacts"
        / "alpha-research"
        / "archive"
        / "halted-2026-04-19-inst-options"
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
