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
    researcher_role = tmp_path / ".agent" / "teams" / "alpha-research" / "roles" / "researcher.md"
    researcher_role.parent.mkdir(parents=True)
    researcher_role.write_text("Cost-Source Gate: TXF ~3 pt, TMF ~4 pt.\n", encoding="utf-8")
    da_role = researcher_role.parent / "devils-advocate.md"
    da_role.write_text("Verify RT base against memory/feedback_taifex_fee_structure.md.\n", encoding="utf-8")
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
                ".agent/teams/alpha-research/roles/researcher.md",
                ".agent/teams/alpha-research/roles/devils-advocate.md",
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
                "config/research/cost_profiles.yaml",
            ],
            "repair_action": (
                "Recover the 2026-04-19 shared-context cost-model snapshot or promote a dated "
                "cost-model provenance note before resolving this institutional-estimate reference."
            ),
        }
    }
    assert warnings == ["Some manifest paper_refs are not mapped in research/knowledge/paper_index.json."]


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
