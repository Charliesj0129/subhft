from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from research.tools.data_governance import validate_metadata_payload

ROOT = Path(__file__).resolve().parent

# Canonical directory layout for research pipeline.
LAYOUT_DIRS: tuple[str, ...] = (
    "alphas",
    "archive",
    "archive/implementations",
    "backtest",
    "combinatorial",
    "data/raw",
    "data/interim",
    "data/processed",
    "data/models",
    "experiments/runs",
    "experiments/comparisons",
    "experiments/validations",
    "experiments/promotions",
    "knowledge/notes",
    "knowledge/papers",
    "knowledge/summaries",
    "knowledge/reports",
    "logs",
    "reports",
    "registry",
    "results",
    "tools",
    "tools/legacy",
    "tools/legacy/rl",
)

GITKEEP_DIRS: tuple[str, ...] = (
    "data/raw",
    "data/interim",
    "data/processed",
    "data/models",
    "experiments/runs",
    "experiments/comparisons",
    "logs",
    "results",
)

ALLOWED_ROOT_DIRS: set[str] = {
    ".benchmarks",
    "__pycache__",  # tolerated; clean command removes it
    "alphas",
    "arxiv_paper",
    "arxiv_papers",
    "archive",
    "backtest",
    "calibration",  # research.calibration package (audit.py, cli.py, replay.py …)
    "combinatorial",
    "data",
    "data_pipeline",  # research.data_pipeline package — canonical L2+tick export contract
    "experiments",
    "knowledge",
    "logs",
    "reports",
    "results_batch6",
    "results_batch7",
    "registry",
    "results",
    "strategy_archive",  # load-bearing: config/live/strategies.yaml + loop_v1 charter pin this path
    "t1",  # research.t1 package — TXF-led mainline (regime_viability.py et al.)
    "templates",  # canonical authoring templates, including strategy_spec.yaml
    "tools",
}

ALLOWED_ROOT_FILES: set[str] = {
    "alpha_analysis.png",
    "alpha_lab.py",
    "__init__.py",
    "__main__.py",
    "README.md",
    "factory.py",
    "pipeline.py",
    "SOP.md",
}

CORE_TOOL_FILES: set[str] = {
    "alpha_scaffold.py",
    "auto_scaffold.py",
    "batch_search.py",
    "alpha_trader_walk_forward.py",
    "batch_alpha_eval.py",
    "bayesian_opt.py",
    "ch_batch_export.py",
    "ch_l2_export.py",
    "cross_signal_explorer.py",
    "data_governance.py",
    "data_ingest.py",
    "data_quality_check.py",
    "depth_alpha_explorer.py",
    "enrich_data_for_alpha.py",
    "eval_batch_3_depth.py",
    "eval_batch_9_price.py",
    "factor_registry.py",
    "feature_benchmark_matrix.py",
    "feature_promotion_check.py",
    "feature_screener.py",
    "fetch_paper.py",
    "hypothesis_queue.py",
    "kalman_filter.py",
    "latency_profiles.py",
    "lifecycle_audit.py",
    "maintenance.py",
    "microstructure_explorer.py",
    "mm_diagnostics.py",
    "mm_param_sweep.py",
    "mm_walk_forward.py",
    "momentum_meanrevert_explorer.py",
    "ofi_alpha_explorer.py",
    "paper_autofill.py",
    "paper_prototype.py",
    "paper_trade.py",
    "prepare_governed_data.py",
    "regime_alpha.py",
    "render_promotion_report.py",
    "run_alpha_trader_backtest.py",
    "run_hftbt_realism_check.py",
    "run_microprice_gates.py",
    "run_microprice_gates_fast.py",
    "run_microprice_promotion.py",
    "run_mm_backtest.py",
    "run_mm_portfolio.py",
    "run_production_alpha_screen.py",
    "run_recent_alpha_backtests.py",
    "spread_microprice_explorer.py",
    "synth_lob_gen.py",
    "toxicity_alpha_explorer.py",
    "toxicity_ii_explorer.py",
    "vm_ul.py",
    "volatility_regime_explorer.py",
    "volume_proxy_explorer.py",
}

LEGACY_ALPHA_REPORT_FILES: set[str] = {
    "feasibility_report.json",
    "correctness_report.json",
    "backtest_report.json",
    "scorecard.json",
    "integration_report.json",
    "promotion_decision.json",
}


@dataclass(frozen=True)
class AuditResult:
    generated_at: str
    errors: list[str]
    warnings: list[str]
    details: dict[str, Any]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "details": self.details,
        }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def cmd_init(_: argparse.Namespace) -> int:
    created: list[str] = []
    for rel in LAYOUT_DIRS:
        target = ROOT / rel
        if not target.exists():
            created.append(str(target.relative_to(ROOT)))
        target.mkdir(parents=True, exist_ok=True)

    for rel in GITKEEP_DIRS:
        marker = ROOT / rel / ".gitkeep"
        marker.parent.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            marker.write_text("")

    print(f"[research.factory] initialized layout under {ROOT}")
    if created:
        print("[research.factory] created directories:")
        for item in created:
            print(f"  - {item}")
    else:
        print("[research.factory] layout already present")
    return 0


def cmd_clean(args: argparse.Namespace) -> int:
    dry_run = bool(args.dry_run)
    removed: list[str] = []

    for cache_dir in ROOT.rglob("__pycache__"):
        if not cache_dir.is_dir():
            continue
        rel = str(cache_dir.relative_to(ROOT))
        if dry_run:
            removed.append(rel + "/")
            continue
        shutil.rmtree(cache_dir, ignore_errors=True)
        removed.append(rel + "/")

    patterns = ("*.pyc", "*.pyo", "*.nbi", "*.nbc")
    for pattern in patterns:
        for fpath in ROOT.rglob(pattern):
            if not fpath.is_file():
                continue
            rel = str(fpath.relative_to(ROOT))
            if dry_run:
                removed.append(rel)
                continue
            fpath.unlink(missing_ok=True)
            removed.append(rel)

    if dry_run:
        print(f"[research.factory] clean dry-run: {len(removed)} items matched")
    else:
        print(f"[research.factory] cleaned {len(removed)} items")

    for item in sorted(removed):
        print(f"  - {item}")
    return 0


def cmd_converge_tools(args: argparse.Namespace) -> int:
    dry_run = bool(args.dry_run)
    tools_root = ROOT / "tools"
    legacy_root = tools_root / "legacy"
    legacy_root.mkdir(parents=True, exist_ok=True)

    moved: list[tuple[str, str]] = []
    for file_path in sorted(tools_root.glob("*.py")):
        if file_path.name in CORE_TOOL_FILES:
            continue
        target = legacy_root / file_path.name
        moved.append((str(file_path.relative_to(ROOT)), str(target.relative_to(ROOT))))
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(file_path), str(target))

    mode = "dry-run" if dry_run else "applied"
    print(f"[research.factory] converge-tools {mode}: moved={len(moved)}")
    for src, dst in moved:
        print(f"  - {src} -> {dst}")
    return 0


def _audit_root_layout(errors: list[str], details: dict[str, Any]) -> None:
    unexpected_files: list[str] = []
    unexpected_dirs: list[str] = []
    for path in sorted(ROOT.iterdir()):
        name = path.name
        if path.is_dir():
            if name not in ALLOWED_ROOT_DIRS:
                unexpected_dirs.append(name)
            continue
        if name not in ALLOWED_ROOT_FILES:
            unexpected_files.append(name)

    details["unexpected_root_files"] = unexpected_files
    details["unexpected_root_dirs"] = unexpected_dirs
    if unexpected_files:
        errors.append(
            "Unexpected files at research root: "
            + ", ".join(unexpected_files)
            + ". Move runnable scripts to research/tools/ or archive/"
        )
    if unexpected_dirs:
        errors.append(
            "Unexpected directories at research root: "
            + ", ".join(unexpected_dirs)
            + ". Keep root folders aligned with the canonical layout."
        )


def _audit_alpha_contract(errors: list[str], warnings: list[str], details: dict[str, Any]) -> None:
    alphas_root = ROOT / "alphas"
    if not alphas_root.exists():
        errors.append("Missing research/alphas directory.")
        details["alpha_contract"] = {}
        return

    missing_required: dict[str, list[str]] = {}
    missing_tests: dict[str, str] = {}
    for alpha_dir in sorted(alphas_root.iterdir()):
        if not alpha_dir.is_dir():
            continue
        if alpha_dir.name.startswith("_") or alpha_dir.name == "__pycache__":
            continue
        required = (
            alpha_dir / "__init__.py",
            alpha_dir / "impl.py",
            alpha_dir / "README.md",
            alpha_dir / "tests",
        )
        missing = [str(p.relative_to(ROOT)) for p in required if not p.exists()]
        if missing:
            missing_required[alpha_dir.name] = missing
            continue
        test_files = list((alpha_dir / "tests").glob("test_*.py"))
        if not test_files:
            missing_tests[alpha_dir.name] = str((alpha_dir / "tests").relative_to(ROOT))

    # Second pass: detect ungoverned flat .py files at the alphas/ root level.
    ungoverned_scripts = [
        p.name for p in sorted(alphas_root.iterdir()) if p.is_file() and p.suffix == ".py" and p.name != "__init__.py"
    ]
    if ungoverned_scripts:
        warnings.append(
            "Ungoverned flat scripts in research/alphas/: "
            + ", ".join(ungoverned_scripts)
            + ". Move to research/archive/implementations/ or scaffold as governed alphas."
        )

    # Third pass: check skills_used / roles_used in manifests via AlphaRegistry.
    alphas_missing_skills: list[str] = []
    alphas_missing_roles: list[str] = []
    try:
        from research.registry.alpha_registry import AlphaRegistry

        registry = AlphaRegistry()
        loaded = registry.discover(alphas_root)
        for alpha_id, alpha_obj in loaded.items():
            manifest = alpha_obj.manifest
            if not getattr(manifest, "skills_used", ()):
                alphas_missing_skills.append(alpha_id)
            if not getattr(manifest, "roles_used", ()):
                alphas_missing_roles.append(alpha_id)
    except Exception:
        pass  # registry errors are surfaced separately

    if alphas_missing_skills:
        warnings.append(
            "Manifest skills_used is empty for: "
            + ", ".join(sorted(alphas_missing_skills))
            + ". Add skill attribution per SOP Stage 2 (iterative-retrieval, hft-backtest-engine, etc.)."
        )
    if alphas_missing_roles:
        warnings.append(
            "Manifest roles_used is empty for: "
            + ", ".join(sorted(alphas_missing_roles))
            + ". Add role attribution per SOP Stage 2 (planner, code-reviewer, etc.)."
        )

    details["alpha_contract"] = {
        "missing_required": missing_required,
        "missing_test_files": missing_tests,
        "ungoverned_flat_scripts": ungoverned_scripts,
        "alphas_missing_skills": sorted(alphas_missing_skills),
        "alphas_missing_roles": sorted(alphas_missing_roles),
    }
    if missing_required:
        errors.append("Some alphas do not satisfy required artifact files.")
    if missing_tests:
        warnings.append("Some alphas have tests/ but no test_*.py files.")


def _audit_alpha_generated_artifacts(errors: list[str], details: dict[str, Any]) -> None:
    alphas_root = ROOT / "alphas"
    if not alphas_root.exists():
        details["legacy_alpha_reports"] = {}
        return

    legacy_hits: dict[str, list[str]] = {}
    for alpha_dir in sorted(alphas_root.iterdir()):
        if not alpha_dir.is_dir():
            continue
        if alpha_dir.name.startswith("_") or alpha_dir.name == "__pycache__":
            continue
        hits = [
            str((alpha_dir / name).relative_to(ROOT))
            for name in sorted(LEGACY_ALPHA_REPORT_FILES)
            if (alpha_dir / name).exists()
        ]
        if hits:
            legacy_hits[alpha_dir.name] = hits

    details["legacy_alpha_reports"] = legacy_hits
    if legacy_hits:
        errors.append(
            "Legacy report files found under research/alphas. "
            "Move generated reports to research/experiments/{validations,runs,promotions}."
        )


def _audit_tools_layout(errors: list[str], details: dict[str, Any]) -> None:
    tools_root = ROOT / "tools"
    unexpected_root_scripts: list[str] = []
    for file_path in sorted(tools_root.glob("*.py")):
        if file_path.name not in CORE_TOOL_FILES:
            unexpected_root_scripts.append(str(file_path.relative_to(ROOT)))

    details["tools_layout"] = {
        "core_tool_files": sorted(CORE_TOOL_FILES),
        "unexpected_root_scripts": unexpected_root_scripts,
    }
    if unexpected_root_scripts:
        errors.append(
            "Non-core scripts found under research/tools root. "
            "Move them to research/tools/legacy (use `python -m research.factory converge-tools`)."
        )


def _audit_paper_refs(warnings: list[str], details: dict[str, Any]) -> None:
    unresolved: dict[str, list[str]] = {}
    unresolved_classes: dict[str, list[dict[str, str]]] = {}
    unresolved_local_research_refs: set[str] = set()
    resolved_local_research_refs: dict[str, str] = {}
    index_path = ROOT / "knowledge" / "paper_index.json"
    if not index_path.exists():
        warnings.append("Missing research/knowledge/paper_index.json; paper reference mapping cannot be verified.")
        details["unresolved_paper_refs"] = unresolved
        details["unresolved_paper_ref_classes"] = unresolved_classes
        details["local_research_ref_repair_hints"] = {}
        details["resolved_local_research_refs"] = resolved_local_research_refs
        return

    try:
        payload = json.loads(index_path.read_text())
        known_refs = _paper_index_aliases(payload)
    except (OSError, ValueError):
        warnings.append("Invalid research/knowledge/paper_index.json; failed to parse paper refs.")
        details["unresolved_paper_refs"] = unresolved
        details["unresolved_paper_ref_classes"] = unresolved_classes
        details["local_research_ref_repair_hints"] = {}
        details["resolved_local_research_refs"] = resolved_local_research_refs
        return

    local_alpha_refs = {
        path.name
        for root in (ROOT / "alphas", ROOT / "archive")
        if root.exists()
        for path in root.rglob("*")
        if path.is_dir() and (path / "manifest.yaml").exists()
    }
    local_research_refs = _local_research_ref_aliases(ROOT.parent)

    try:
        for manifest_path in sorted((ROOT / "alphas").glob("*/manifest.yaml")):
            if manifest_path.parent.name.startswith("_"):
                continue
            data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                warnings.append(f"Invalid alpha manifest shape: {manifest_path.relative_to(ROOT)}")
                continue
            alpha_id = str(data.get("alpha_id") or manifest_path.parent.name)
            refs = [str(ref) for ref in data.get("paper_refs", ())]
            unknown: list[str] = []
            for ref in refs:
                if not ref:
                    continue
                if _paper_ref_resolved(ref, known_refs, local_alpha_refs, local_research_refs):
                    if ref in local_research_refs:
                        resolved_local_research_refs.setdefault(ref, local_research_refs[ref])
                    continue
                unknown.append(ref)
            if unknown:
                unresolved[alpha_id] = unknown
                classifications: list[dict[str, str]] = []
                for ref in unknown:
                    reason = _classify_unresolved_paper_ref(ref)
                    classifications.append({"ref": ref, "reason": reason})
                    if reason == "local_research_ref_not_indexed":
                        unresolved_local_research_refs.add(ref)
                unresolved_classes[alpha_id] = classifications
    except Exception as exc:
        warnings.append(f"Failed to audit manifest paper_refs: {exc}")

    details["unresolved_paper_refs"] = unresolved
    details["unresolved_paper_ref_classes"] = unresolved_classes
    details["local_research_ref_repair_hints"] = _local_research_ref_repair_hints(
        ROOT.parent,
        unresolved_local_research_refs,
    )
    details["resolved_local_research_refs"] = dict(sorted(resolved_local_research_refs.items()))
    if unresolved:
        warnings.append("Some manifest paper_refs are not mapped in research/knowledge/paper_index.json.")


_ARXIV_ID_RE = re.compile(r"(?P<id>\d{4}\.\d{4,5})(?:v\d+)?")

_LOCAL_RESEARCH_REF_ALIASES: dict[str, str] = {
    "AMHP-2024": "docs/alpha-research/round-1-hawkes-amhp/artifacts/t1_researcher_c1.md",
    "c13_vol_gate_disable_R7_kill": (
        "outputs/team_artifacts/alpha-research/archive/"
        "halted-2026-04-18-pre-B-C/round-7/artifacts/t1_researcher_proposal.md"
    ),
    "feedback_taifex_fee_structure": (
        "outputs/team_artifacts/alpha-research/archive/"
        "halted-2026-04-18-pre-B-C/round-6/summary.md"
    ),
    "memory/backtest_method_reliability": "docs/runbooks/backtest-engine-selection.md",
    "r47_backtest_data_regression": "docs/incidents/2026-04-24-r47-backtest-credibility-audit.md",
    "r47_maker_strategy": "research/alphas/r47_maker_pivot/manifest.yaml",
    "r47_structural_properties": ".agent/skills/hft-mm-design/SKILL.md",
    "r47_tmfd6_economics": "outputs/team_artifacts/alpha-research/r47_tmfd6_economics.md",
    "r7_summary C66 hedge-cost-dominance lesson": (
        "outputs/team_artifacts/alpha-research/archive/"
        "halted-2026-04-19-inst-options/round-7/summary.md"
    ),
    "shared-context_2026-04-19_cost_model": (
        "outputs/team_artifacts/alpha-research/archive/"
        "halted-2026-04-19-inst-options/final_summary.md"
    ),
}

_LOCAL_RESEARCH_REF_REPAIR_HINTS: dict[str, dict[str, Any]] = {
    "feedback_taifex_fee_structure": {
        "missing_path": "memory/feedback_taifex_fee_structure.md",
        "candidate_paths": (
            "config/research/cost_profiles.yaml",
            ".agent/teams/alpha-research/roles/researcher.md",
            ".agent/teams/alpha-research/roles/devils-advocate.md",
            "research/alphas/c1_revalidation_txfd6_chavez_casillas_adaptive/manifest.yaml",
            "research/alphas/c30_txf_maker_tmf_hedge_pair/manifest.yaml",
            "research/alphas/c32b_tob_survival_refresh_regime_gate/manifest.yaml",
            "research/alphas/c33_txfd6_solo_passive_maker/manifest.yaml",
        ),
        "repair_action": (
            "Restore the missing memory file or promote one current cost-source gate artifact "
            "before resolving this cost-related reference."
        ),
    },
    "shared-context_2026-04-19_cost_model": {
        "missing_path": "shared-context_2026-04-19_cost_model",
        "candidate_paths": (
            "outputs/team_artifacts/alpha-research/archive/"
            "halted-2026-04-19-inst-options/candidate_pool.json",
            "outputs/team_artifacts/alpha-research/archive/"
            "halted-2026-04-19-inst-options/progress.jsonl",
            "outputs/team_artifacts/alpha-research/archive/"
            "halted-2026-04-19-inst-options/final_summary.md",
            "config/research/cost_profiles.yaml",
            "research/alphas/c60_tmfd6_r47_minimal_inst_rt/manifest.yaml",
            "research/alphas/c63_txfd6_r47_tight_spread/manifest.yaml",
            "research/alphas/c68_txf_rollover_back_front_maker/manifest.yaml",
            "research/alphas/c72_tmfd6_queue_position_aware/manifest.yaml",
            "research/alphas/c74_txf_tmf_basis_mean_reversion/manifest.yaml",
        ),
        "repair_action": (
            "Recover the 2026-04-19 shared-context cost-model snapshot or promote a dated "
            "cost-model provenance note before resolving this institutional-estimate reference."
        ),
    },
}


def _paper_index_aliases(payload: Any) -> set[str]:
    aliases: set[str] = set()
    if not isinstance(payload, dict):
        return aliases
    for key, value in payload.items():
        aliases.add(str(key))
        if not isinstance(value, dict):
            continue
        for field in ("ref", "arxiv_id", "title"):
            raw = value.get(field)
            if raw:
                text = str(raw)
                aliases.add(text)
                match = _ARXIV_ID_RE.search(text)
                if match:
                    aliases.add(match.group("id"))
        raw_aliases = value.get("aliases", ())
        if isinstance(raw_aliases, str):
            raw_aliases = (raw_aliases,)
        if isinstance(raw_aliases, list | tuple):
            for raw_alias in raw_aliases:
                if raw_alias:
                    aliases.add(str(raw_alias))
    return aliases


def _local_research_ref_aliases(project_root: Path) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for ref, rel_path in _LOCAL_RESEARCH_REF_ALIASES.items():
        if (project_root / rel_path).exists():
            aliases[ref] = rel_path
    return aliases


def _local_research_ref_repair_hints(project_root: Path, refs: set[str]) -> dict[str, dict[str, Any]]:
    hints: dict[str, dict[str, Any]] = {}
    for ref in sorted(refs):
        hint = _LOCAL_RESEARCH_REF_REPAIR_HINTS.get(ref)
        if not hint:
            continue
        candidate_paths = [
            rel_path
            for rel_path in hint.get("candidate_paths", ())
            if (project_root / str(rel_path)).exists()
        ]
        hints[ref] = {
            "missing_path": str(hint["missing_path"]),
            "candidate_paths": candidate_paths,
            "repair_action": str(hint["repair_action"]),
        }
    return hints


def _paper_ref_resolved(
    ref: str,
    known_refs: set[str],
    local_alpha_refs: set[str],
    local_research_refs: dict[str, str],
) -> bool:
    if ref in known_refs or ref in local_alpha_refs or ref in local_research_refs:
        return True
    match = _ARXIV_ID_RE.search(ref)
    return bool(match and match.group("id") in known_refs)


def _classify_unresolved_paper_ref(ref: str) -> str:
    if _ARXIV_ID_RE.search(ref):
        return "arxiv_ref_not_indexed"
    if (
        ref.startswith("memory/")
        or ref.startswith("shared-context_")
        or ref.startswith("feedback_")
        or re.match(r"^[cr]\d+[_-]", ref)
    ):
        return "local_research_ref_not_indexed"
    return "external_citation_not_indexed"


def _audit_binary_pollution(warnings: list[str], details: dict[str, Any]) -> None:
    binary_ext = {".onnx", ".zip", ".npy", ".npz", ".parquet", ".data"}
    source_roots = (ROOT / "alphas", ROOT / "backtest", ROOT / "registry", ROOT / "tools")
    hits: list[str] = []
    for base in source_roots:
        if not base.exists():
            continue
        for file_path in base.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix.lower() in binary_ext:
                if _is_allowed_research_binary_fixture(file_path):
                    continue
                hits.append(str(file_path.relative_to(ROOT)))
    details["binary_pollution_in_source_zones"] = sorted(hits)
    if hits:
        warnings.append("Binary artifacts detected in source zones; move to research/data or research/archive.")


def _is_allowed_research_binary_fixture(file_path: Path) -> bool:
    return file_path.parent == ROOT / "backtest" / "q_hat_data" and file_path.suffix.lower() == ".parquet"


def _dataset_metadata_candidates(data_path: Path) -> tuple[Path, ...]:
    return (
        data_path.with_suffix(data_path.suffix + ".meta.json"),
        data_path.with_suffix(".meta.json"),
        data_path.with_suffix(data_path.suffix + ".metadata.json"),
        data_path.with_suffix(".metadata.json"),
    )


def _format_dataset_label(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _resolve_scoped_governed_datasets(
    data_paths: list[str],
    governed_roots: tuple[Path, ...],
    governed_ext: set[str],
) -> list[Path]:
    out: list[Path] = []
    for raw in data_paths:
        p = Path(raw)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        else:
            p = p.resolve()
        if not p.is_file():
            continue
        if p.suffix.lower() not in governed_ext:
            continue
        if not any(base in p.parents for base in governed_roots):
            continue
        out.append(p)
    unique = {str(path): path for path in out}
    return sorted(unique.values())


def _audit_data_governance(
    errors: list[str],
    details: dict[str, Any],
    *,
    data_paths: list[str] | None = None,
) -> None:
    governed_roots = (
        ROOT / "data" / "raw",
        ROOT / "data" / "interim",
        ROOT / "data" / "processed",
    )
    governed_ext = {".npy", ".npz"}
    scanned: list[str] = []
    missing_meta: list[str] = []
    invalid_meta: dict[str, list[str]] = {}

    scoped = list(data_paths or [])
    if scoped:
        datasets = _resolve_scoped_governed_datasets(scoped, governed_roots, governed_ext)
    else:
        datasets = []
        for base in governed_roots:
            if not base.exists():
                continue
            for file_path in sorted(base.rglob("*")):
                if not file_path.is_file():
                    continue
                if file_path.name.startswith("."):
                    continue
                if file_path.suffix.lower() not in governed_ext:
                    continue
                datasets.append(file_path)

    for file_path in datasets:
        label = _format_dataset_label(file_path)
        scanned.append(label)
        meta_path = next((p for p in _dataset_metadata_candidates(file_path) if p.exists()), None)
        if meta_path is None:
            missing_meta.append(label)
            continue
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            invalid_meta[label] = [f"invalid_json:{exc}"]
            continue
        try:
            arr = np.load(file_path, allow_pickle=False)
            if isinstance(arr, np.lib.npyio.NpzFile):
                try:
                    if "data" in arr:
                        loaded = np.asarray(arr["data"])
                    elif arr.files:
                        loaded = np.asarray(arr[arr.files[0]])
                    else:
                        loaded = np.asarray([], dtype=np.float64)
                finally:
                    arr.close()
            else:
                loaded = np.asarray(arr)
        except Exception as exc:
            invalid_meta[label] = [f"dataset_load_error:{exc}"]
            continue
        problems = validate_metadata_payload(payload, loaded)
        if problems:
            invalid_meta[label] = problems

    details["data_governance"] = {
        "scope": ("scoped_data_paths" if scoped else "all_governed_roots"),
        "governed_roots": [str(p.relative_to(ROOT)) for p in governed_roots],
        "scanned_datasets": scanned,
        "missing_metadata_sidecars": missing_meta,
        "invalid_metadata_sidecars": invalid_meta,
    }
    if missing_meta:
        errors.append("Data governance violation: metadata sidecar missing for dataset(s): " + ", ".join(missing_meta))
    if invalid_meta:
        bad = ", ".join(f"{path}({';'.join(problems)})" for path, problems in sorted(invalid_meta.items()))
        errors.append("Data governance violation: metadata sidecar invalid for dataset(s): " + bad)


def _audit_experiment_edge_metric_semantics(details: dict[str, Any]) -> None:
    """Audit Gate-C edge artifacts for trustworthy edge semantics.

    Buckets each artifact carrying an ``edge_per_round_trip`` metric into:
    ``missing_semantics`` (label absent, predates stamping), ``unvalidated``
    (label present but a supporting gate failed/absent — e.g. a residual-propped
    or single-day-dominated edge), or ``complete`` (label present and validated).
    """
    experiments_root = ROOT / "experiments"
    report_paths = sorted(experiments_root.glob("**/backtest_report.json")) if experiments_root.exists() else []
    missing_semantics: list[dict[str, Any]] = []
    unvalidated: list[dict[str, Any]] = []
    complete: list[dict[str, str]] = []
    parse_errors: list[str] = []
    reports_with_edge_gate = 0

    for report_path in report_paths:
        report = _load_json_object(report_path)
        report_rel = str(report_path.relative_to(ROOT))
        if report is None:
            parse_errors.append(report_rel)
            continue
        if not _contains_edge_round_trip_metric(report):
            continue

        reports_with_edge_gate += 1
        scorecard_path = report_path.parent / "scorecard.json"
        scorecard = _load_json_object(scorecard_path) if scorecard_path.exists() else None
        scorecard_rel = str(scorecard_path.relative_to(ROOT))
        missing: list[str] = []
        if not _contains_edge_metric_semantics(scorecard):
            missing.append("scorecard.edge_metric_semantics")
        if not _contains_edge_metric_semantics(report):
            missing.append("report.edge_metric_semantics")

        row = {
            "run_dir": str(report_path.parent.relative_to(ROOT)),
            "report_path": report_rel,
            "scorecard_path": scorecard_rel,
        }
        if missing:
            missing_semantics.append({**row, "missing": missing})
            continue

        validated, failing_gates = _edge_metric_semantics_validation(scorecard)
        if not validated:
            unvalidated.append({**row, "failing_gates": failing_gates})
        else:
            complete.append(row)

    details["experiment_edge_metric_semantics"] = {
        "scanned_reports": len(report_paths),
        "reports_with_edge_gate": reports_with_edge_gate,
        "missing_semantics": missing_semantics,
        "unvalidated": unvalidated,
        "complete": complete,
        "parse_errors": parse_errors,
    }


def _audit_experiment_research_decisions(details: dict[str, Any]) -> None:
    """Report Gate-C experiment metadata that cannot replay keep/kill decisions."""
    experiments_root = ROOT / "experiments"
    meta_paths = sorted(experiments_root.glob("**/meta.json")) if experiments_root.exists() else []
    missing_decisions: list[dict[str, Any]] = []
    derivable_decisions: list[dict[str, Any]] = []
    not_derivable_decisions: list[dict[str, str]] = []
    complete: list[dict[str, str]] = []
    parse_errors: list[str] = []
    gate_c_runs = 0

    for meta_path in meta_paths:
        meta = _load_json_object(meta_path)
        meta_rel = _rel_to_root(meta_path)
        if meta is None:
            parse_errors.append(meta_rel)
            continue

        report_path = _experiment_report_path(meta_path, meta)
        report = _load_json_object(report_path) if report_path.exists() else None
        if not _is_gate_c_experiment(meta, report):
            continue

        gate_c_runs += 1
        row = {
            "run_dir": _rel_to_root(meta_path.parent),
            "meta_path": meta_rel,
            "report_path": _rel_to_root(report_path),
        }
        missing = _research_decision_missing_fields(meta.get("research_decision"))
        if missing:
            missing_decisions.append({**row, "missing": missing})
            derived = _derive_gate_c_research_decision(report)
            if derived:
                derivable_decisions.append({**row, "research_decision": derived})
            else:
                not_derivable_decisions.append({**row, "reason": "missing_gate_c_blocking_evidence"})
        else:
            decision = meta["research_decision"]
            complete.append(
                {
                    **row,
                    "status": str(decision["status"]),
                    "reason": str(decision["reason"]),
                }
            )

    details["experiment_research_decisions"] = {
        "scanned_meta": len(meta_paths),
        "gate_c_runs": gate_c_runs,
        "missing_decisions": missing_decisions,
        "derivable_decisions": derivable_decisions,
        "not_derivable_decisions": not_derivable_decisions,
        "complete": complete,
        "parse_errors": parse_errors,
    }


def cmd_backfill_research_decisions(args: argparse.Namespace) -> int:
    """Build a dry-run plan for safely backfilling replayable research decisions."""
    details: dict[str, Any] = {}
    _audit_experiment_research_decisions(details)
    decision_audit = details["experiment_research_decisions"]
    planned = list(decision_audit["derivable_decisions"])
    skipped = list(decision_audit["not_derivable_decisions"])
    apply_changes = bool(getattr(args, "apply", False))

    payload = {
        "generated_at": _now_iso(),
        "mode": "apply" if apply_changes else "dry_run",
        "apply": apply_changes,
        "planned_count": len(planned),
        "skipped_count": len(skipped),
        "planned": planned,
        "skipped": skipped,
    }
    out_path = (
        Path(args.out).resolve()
        if getattr(args, "out", "")
        else (ROOT / "reports" / "research_decision_backfill.json")
    )
    _write_json(out_path, payload)
    print(f"[research.factory] research-decision backfill plan: {out_path}")
    print(
        "[research.factory] "
        f"mode={payload['mode']} planned={payload['planned_count']} skipped={payload['skipped_count']}"
    )
    return 0


def _load_json_object(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _rel_to_root(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _experiment_report_path(meta_path: Path, meta: dict[str, Any]) -> Path:
    raw_path = str(meta.get("backtest_report_path", "") or "").strip()
    if not raw_path:
        return meta_path.parent / "backtest_report.json"

    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    if (ROOT / candidate).exists():
        return ROOT / candidate
    if (ROOT.parent / candidate).exists():
        return ROOT.parent / candidate
    return meta_path.parent / candidate


def _is_gate_c_experiment(meta: dict[str, Any], report: dict[str, Any] | None) -> bool:
    gate_status = meta.get("gate_status")
    if isinstance(gate_status, dict) and "gate_c" in gate_status:
        return True
    if report is None:
        return False
    if str(report.get("gate", "")).strip().lower() == "gate c":
        return True
    return _contains_gate_c_sub_gate_payload(report)


def _contains_gate_c_sub_gate_payload(value: Any) -> bool:
    if isinstance(value, dict):
        if "sub_gates_blocking" in value or "sub_gates_advisory" in value:
            return True
        return any(_contains_gate_c_sub_gate_payload(v) for v in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_gate_c_sub_gate_payload(v) for v in value)
    return False


def _research_decision_missing_fields(payload: Any) -> list[str]:
    if not isinstance(payload, dict) or not payload:
        return ["meta.research_decision"]
    missing: list[str] = []
    if not str(payload.get("status", "")).strip():
        missing.append("meta.research_decision.status")
    if not str(payload.get("reason", "")).strip():
        missing.append("meta.research_decision.reason")
    return missing


def _derive_gate_c_research_decision(report: dict[str, Any] | None) -> dict[str, Any]:
    if report is None:
        return {}
    from hft_platform.alpha.experiments import derive_research_decision_from_gate_c_report

    return derive_research_decision_from_gate_c_report(report)


def _contains_edge_round_trip_metric(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("name") == "edge_per_round_trip":
            metrics = value.get("metrics")
            return isinstance(metrics, dict) and "mean_net_edge_pts_per_trade" in metrics
        return any(_contains_edge_round_trip_metric(v) for v in value.values())
    if isinstance(value, list | tuple):
        return any(_contains_edge_round_trip_metric(v) for v in value)
    return False


def _contains_edge_metric_semantics(value: Any) -> bool:
    return _find_edge_metric_semantics(value) is not None


def _find_edge_metric_semantics(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        semantics = value.get("edge_metric_semantics")
        if isinstance(semantics, dict) and (
            semantics.get("schema") == "edge_metric_semantics.v1"
            and semantics.get("metric") == "mean_net_edge_pts_per_trade"
            and semantics.get("source_gate") == "edge_per_round_trip"
        ):
            return semantics
        for v in value.values():
            found = _find_edge_metric_semantics(v)
            if found is not None:
                return found
        return None
    if isinstance(value, list | tuple):
        for v in value:
            found = _find_edge_metric_semantics(v)
            if found is not None:
                return found
    return None


def _edge_metric_semantics_validation(value: Any) -> tuple[bool, list[str]]:
    """Return ``(validated, failing_gates)`` for a stamped edge-semantics label.

    Mirrors ``hft_platform.alpha.experiments``: a label proves trustworthiness
    only when ``validated is True``. Labels stamped before evidence was recorded
    (no ``validated`` key) are treated as unvalidated.
    """
    semantics = _find_edge_metric_semantics(value)
    if semantics is None:
        return False, []
    status_map = semantics.get("supporting_gates_status")
    failing: list[str] = []
    if isinstance(status_map, dict):
        failing = sorted(str(gate) for gate, status in status_map.items() if str(status) != "pass")
    return semantics.get("validated") is True, failing


def cmd_audit(args: argparse.Namespace) -> int:
    errors: list[str] = []
    warnings: list[str] = []
    details: dict[str, Any] = {}

    _audit_root_layout(errors, details)
    _audit_alpha_contract(errors, warnings, details)
    _audit_alpha_generated_artifacts(errors, details)
    _audit_tools_layout(errors, details)
    _audit_paper_refs(warnings, details)
    _audit_binary_pollution(warnings, details)
    _audit_data_governance(errors, details, data_paths=list(getattr(args, "data", []) or []))
    _audit_experiment_edge_metric_semantics(details)
    _audit_experiment_research_decisions(details)

    result = AuditResult(
        generated_at=_now_iso(),
        errors=errors,
        warnings=warnings,
        details=details,
    )
    payload = result.to_dict()

    out_path = Path(args.out).resolve() if args.out else (ROOT / "reports" / "factory_audit.json")
    _write_json(out_path, payload)
    print(f"[research.factory] audit report: {out_path}")
    print(f"[research.factory] status={'OK' if result.ok else 'FAIL'} errors={len(errors)} warnings={len(warnings)}")

    if args.fail_on_warning and warnings:
        return 1
    return 0 if result.ok else 1


def _load_experiment_stats() -> dict[str, Any]:
    runs_root = ROOT / "experiments" / "runs"
    per_alpha: dict[str, dict[str, Any]] = {}
    if not runs_root.exists():
        return per_alpha

    for meta_path in sorted(runs_root.glob("*/meta.json")):
        try:
            payload = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            continue
        alpha_id = str(payload.get("alpha_id", "")).strip()
        run_id = str(payload.get("run_id", "")).strip()
        ts = str(payload.get("timestamp", "")).strip()
        if not alpha_id:
            continue
        row = per_alpha.setdefault(alpha_id, {"run_count": 0, "latest_run_id": "", "latest_timestamp": ""})
        row["run_count"] = int(row["run_count"]) + 1
        if ts and ts >= str(row["latest_timestamp"]):
            row["latest_timestamp"] = ts
            row["latest_run_id"] = run_id
    return per_alpha


def cmd_index(args: argparse.Namespace) -> int:
    from research.registry.alpha_registry import AlphaRegistry

    registry = AlphaRegistry()
    loaded = registry.discover(ROOT / "alphas")
    manifests = []
    for alpha_id in sorted(loaded):
        manifest = loaded[alpha_id].manifest
        manifests.append(
            {
                "alpha_id": manifest.alpha_id,
                "status": manifest.status.value,
                "tier": manifest.tier.value if manifest.tier else None,
                "complexity": manifest.complexity,
                "paper_refs": list(manifest.paper_refs),
                "data_fields": list(manifest.data_fields),
                "rust_module": manifest.rust_module,
                "latency_profile": manifest.latency_profile,
            }
        )

    exp_stats = _load_experiment_stats()
    for item in manifests:
        stats = exp_stats.get(item["alpha_id"], {})
        item["run_count"] = int(stats.get("run_count", 0))
        item["latest_run_id"] = str(stats.get("latest_run_id", ""))
        item["latest_timestamp"] = str(stats.get("latest_timestamp", ""))

    out_path = Path(args.out).resolve() if args.out else (ROOT / "reports" / "pipeline_index.json")
    payload = {
        "generated_at": _now_iso(),
        "alpha_count": len(manifests),
        "registry_errors": list(registry.errors),
        "alphas": manifests,
    }
    _write_json(out_path, payload)
    print(f"[research.factory] index written: {out_path}")
    return 0


def cmd_optimize(args: argparse.Namespace) -> int:
    report_path = Path(args.out).resolve() if args.out else (ROOT / "reports" / "factory_optimize.json")
    audit_path = Path(args.audit_out).resolve() if args.audit_out else (ROOT / "reports" / "factory_audit.json")
    index_path = Path(args.index_out).resolve() if args.index_out else (ROOT / "reports" / "pipeline_index.json")

    started_at = _now_iso()
    steps: list[dict[str, Any]] = []

    def run_step(step: str, fn: Any, ns: argparse.Namespace) -> int:
        rc = int(fn(ns))
        steps.append({"step": step, "rc": rc, "ok": rc == 0})
        return rc

    init_rc = run_step("init", cmd_init, argparse.Namespace())
    if init_rc != 0:
        payload = {
            "started_at": started_at,
            "finished_at": _now_iso(),
            "ok": False,
            "steps": steps,
            "artifacts": {"audit_report": str(audit_path), "index_report": str(index_path)},
        }
        _write_json(report_path, payload)
        print(f"[research.factory] optimize report: {report_path}")
        return init_rc

    converge_rc = run_step("converge_tools", cmd_converge_tools, argparse.Namespace(dry_run=False))
    if converge_rc != 0:
        payload = {
            "started_at": started_at,
            "finished_at": _now_iso(),
            "ok": False,
            "steps": steps,
            "artifacts": {"audit_report": str(audit_path), "index_report": str(index_path)},
        }
        _write_json(report_path, payload)
        print(f"[research.factory] optimize report: {report_path}")
        return converge_rc

    if not args.skip_clean:
        clean_rc = run_step("clean", cmd_clean, argparse.Namespace(dry_run=False))
        if clean_rc != 0:
            payload = {
                "started_at": started_at,
                "finished_at": _now_iso(),
                "ok": False,
                "steps": steps,
                "artifacts": {"audit_report": str(audit_path), "index_report": str(index_path)},
            }
            _write_json(report_path, payload)
            print(f"[research.factory] optimize report: {report_path}")
            return clean_rc

    fail_on_warning = not bool(args.allow_audit_warnings)
    audit_rc = run_step(
        "audit",
        cmd_audit,
        argparse.Namespace(
            out=str(audit_path),
            fail_on_warning=fail_on_warning,
            data=list(getattr(args, "data", []) or []),
        ),
    )
    if audit_rc != 0:
        payload = {
            "started_at": started_at,
            "finished_at": _now_iso(),
            "ok": False,
            "steps": steps,
            "artifacts": {"audit_report": str(audit_path), "index_report": str(index_path)},
            "fail_on_warning": fail_on_warning,
        }
        _write_json(report_path, payload)
        print(f"[research.factory] optimize report: {report_path}")
        return audit_rc

    if not args.skip_index:
        index_rc = run_step("index", cmd_index, argparse.Namespace(out=str(index_path)))
        if index_rc != 0:
            payload = {
                "started_at": started_at,
                "finished_at": _now_iso(),
                "ok": False,
                "steps": steps,
                "artifacts": {"audit_report": str(audit_path), "index_report": str(index_path)},
                "fail_on_warning": fail_on_warning,
            }
            _write_json(report_path, payload)
            print(f"[research.factory] optimize report: {report_path}")
            return index_rc

    payload = {
        "started_at": started_at,
        "finished_at": _now_iso(),
        "ok": True,
        "steps": steps,
        "artifacts": {"audit_report": str(audit_path), "index_report": str(index_path)},
        "fail_on_warning": fail_on_warning,
    }
    _write_json(report_path, payload)
    print(f"[research.factory] optimize report: {report_path}")
    return 0


def cmd_run_gate_c(args: argparse.Namespace) -> int:
    """Run Gate A → B → C for a single alpha and print the scorecard summary."""
    from research.registry.alpha_registry import AlphaRegistry
    from research.tools.latency_profiles import load_latency_profile
    from src.hft_platform.alpha.validation import ValidationConfig, run_gate_a, run_gate_b, run_gate_c

    alpha_id: str = str(args.alpha_id)
    data_paths: list[str] = list(args.data or [])
    oos_split: float = float(args.oos_split)
    latency_profile_id: str = str(args.latency_profile)
    skip_gate_b: bool = bool(getattr(args, "skip_gate_b", False))

    # --- Load latency profile from versioned YAML ---
    try:
        latency = load_latency_profile(latency_profile_id)
    except (KeyError, FileNotFoundError) as exc:
        print(f"[run-gate-c] ERROR: {exc}")
        return 1

    opt_threshold_min: float = float(getattr(args, "opt_threshold_min", 0.01))
    no_opt: bool = bool(getattr(args, "no_opt", False))
    config = ValidationConfig(
        alpha_id=alpha_id,
        data_paths=data_paths,
        is_oos_split=oos_split,
        latency_profile_id=latency_profile_id,
        submit_ack_latency_ms=latency["submit_ack_latency_ms"],
        modify_ack_latency_ms=latency["modify_ack_latency_ms"],
        cancel_ack_latency_ms=latency["cancel_ack_latency_ms"],
        local_decision_pipeline_latency_us=latency["local_decision_pipeline_latency_us"],
        opt_signal_threshold_min=opt_threshold_min,
        enable_param_optimization=not no_opt,
    )

    # --- Discover alpha ---
    registry = AlphaRegistry()
    loaded = registry.discover(ROOT / "alphas")
    if alpha_id not in loaded:
        print(f"[run-gate-c] ERROR: alpha '{alpha_id}' not found in research/alphas/")
        print(f"  Available: {sorted(loaded.keys())}")
        return 1

    alpha_instance = loaded[alpha_id]
    manifest = alpha_instance.manifest

    # Project root is one level above the research/ directory
    project_root = ROOT.parent

    # Resolve data paths relative to project root
    resolved_paths: list[str] = []
    for p in data_paths:
        candidate = Path(p)
        if not candidate.is_absolute():
            candidate = (project_root / p).resolve()
        resolved_paths.append(str(candidate))

    print(f"\n[run-gate-c] ── {alpha_id} ────────────────────────────────────────")
    print(f"  latency_profile : {latency_profile_id}")
    print(f"  submit_ack_ms   : {latency['submit_ack_latency_ms']}")
    print(f"  data_paths      : {resolved_paths}")

    # --- Gate A ---
    gate_a = run_gate_a(manifest, resolved_paths, config=config, root=project_root)
    status_a = "PASS" if gate_a.passed else "FAIL"
    print(f"\n[run-gate-c] Gate A → {status_a}")
    if not gate_a.passed:
        print(f"  details: {gate_a.details}")
        return 1

    # --- Gate B ---
    if skip_gate_b:
        print("[run-gate-c] Gate B → SKIPPED (--skip-gate-b)")
    else:
        gate_b = run_gate_b(alpha_id, project_root)
        status_b = "PASS" if gate_b.passed else "FAIL"
        print(f"[run-gate-c] Gate B → {status_b}")
        if not gate_b.passed:
            print(f"  stdout tail: {gate_b.details.get('stdout_tail', '')}")
            print(f"  stderr tail: {gate_b.details.get('stderr_tail', '')}")
            return 1

    # --- Gate C ---
    experiments_base = ROOT / "experiments"
    gate_c_result = run_gate_c(alpha_instance, config, project_root, resolved_paths, experiments_base)
    gate_c, run_id, config_hash, scorecard_path, experiment_meta_path = gate_c_result
    status_c = "PASS" if gate_c.passed else "FAIL"
    print(f"[run-gate-c] Gate C → {status_c}")
    if scorecard_path:
        print(f"  scorecard : {scorecard_path}")
    if run_id:
        print(f"  run_id    : {run_id}")

    details = gate_c.details
    sharpe_oos = details.get("sharpe_oos")
    sharpe_is = details.get("sharpe_is")
    ic = details.get("ic_mean")
    wf = details.get("walk_forward_consistency_pct")
    regime_sharpe = details.get("regime_sharpe", {})

    print(f"\n  Sharpe IS={sharpe_is!r}  OOS={sharpe_oos!r}  IC={ic!r}  WF-consistency={wf!r}")
    if regime_sharpe:
        print(f"  regime Sharpe: {regime_sharpe}")

    if not gate_c.passed:
        print(f"  gate_c details: {details}")

    overall = gate_c.passed
    print(f"\n[run-gate-c] RESULT: {'PASS ✓' if overall else 'FAIL ✗'}")
    return 0 if overall else 1


def cmd_run_bayesian_opt(args: argparse.Namespace) -> int:
    """Run Bayesian optimization for a single alpha's parameter space."""
    from research.tools.bayesian_opt import BayesianOptConfig, run_bayesian_opt

    alpha_id: str = str(args.alpha_id)
    data_paths: list[str] = list(args.data or [])
    n_trials: int = int(args.n_trials)
    oos_split: float = float(args.oos_split)
    latency_profile_id: str = str(args.latency_profile)
    seed: int | None = int(args.seed) if args.seed is not None else None

    # Parse param space: each --param is "name:lo:hi" or "name:lo:hi:log"
    param_space: dict[str, tuple[float, float, bool]] = {}
    for spec in args.param or []:
        parts = spec.split(":")
        if len(parts) < 3:
            print(f"[run-bayesian-opt] ERROR: invalid --param '{spec}', expected name:lo:hi[:log]")
            return 1
        name = parts[0]
        lo = float(parts[1])
        hi = float(parts[2])
        log_scale = len(parts) >= 4 and parts[3].lower() in ("log", "true", "1")
        param_space[name] = (lo, hi, log_scale)

    config = BayesianOptConfig(
        alpha_id=alpha_id,
        data_paths=data_paths,
        n_trials=n_trials,
        param_space=param_space if param_space else None,
        objective="risk_adjusted",
        seed=seed,
        latency_profile_id=latency_profile_id,
        is_oos_split=oos_split,
    )

    print(f"\n[run-bayesian-opt] ── {alpha_id} ────────────────────────────────────")
    print(f"  n_trials          : {n_trials}")
    print(f"  latency_profile   : {latency_profile_id}")
    print(f"  param_space       : {config.param_space}")
    print(f"  data_paths        : {data_paths}")

    try:
        result = run_bayesian_opt(config)
    except Exception as exc:
        print(f"[run-bayesian-opt] ERROR: {exc}")
        return 1

    print("\n[run-bayesian-opt] RESULT:")
    print(f"  best_params       : {result.best_params}")
    print(f"  best_objective    : {result.best_objective:.4f}")
    print(f"  deflated_sharpe   : {result.deflated_sharpe:.4f}")
    print(f"  param_importance  : {result.param_importance}")

    # Save result JSON
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out_path = ROOT / "experiments" / "runs" / f"{alpha_id}_bayesian_{ts}.json"
    _write_json(out_path, result.to_dict())
    print(f"  saved             : {out_path}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Research pipeline factory for layout, cleanup, audit, and indexing.")
    sub = parser.add_subparsers(dest="command", required=True)

    init_cmd = sub.add_parser("init", help="Initialize canonical research directory layout.")
    init_cmd.set_defaults(func=cmd_init)

    clean_cmd = sub.add_parser("clean", help="Clean generated cache artifacts under research/.")
    clean_cmd.add_argument("--dry-run", action="store_true", help="Only print files/directories that would be removed.")
    clean_cmd.set_defaults(func=cmd_clean)

    converge_tools = sub.add_parser(
        "converge-tools",
        help="Move non-core scripts from research/tools root into research/tools/legacy.",
    )
    converge_tools.add_argument("--dry-run", action="store_true", help="Only show files that would be moved.")
    converge_tools.set_defaults(func=cmd_converge_tools)

    audit_cmd = sub.add_parser("audit", help="Audit research folder for pipeline-contract violations.")
    audit_cmd.add_argument("--out", default="", help="Output json report path.")
    audit_cmd.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Return non-zero when warnings exist.",
    )
    audit_cmd.add_argument(
        "--data",
        nargs="*",
        default=[],
        help="Optional dataset path scope for data-governance audit.",
    )
    audit_cmd.set_defaults(func=cmd_audit)

    backfill_decisions_cmd = sub.add_parser(
        "backfill-research-decisions",
        help="Write a dry-run plan for safely backfilling Gate-C research_decision metadata.",
    )
    backfill_decisions_cmd.add_argument("--out", default="", help="Output json plan path.")
    backfill_decisions_cmd.set_defaults(func=cmd_backfill_research_decisions, apply=False)

    index_cmd = sub.add_parser("index", help="Build machine-readable alpha pipeline index.")
    index_cmd.add_argument("--out", default="", help="Output json path.")
    index_cmd.set_defaults(func=cmd_index)

    optimize_cmd = sub.add_parser(
        "optimize",
        help="One-flow factory pipeline: init -> converge-tools -> clean -> audit -> index.",
    )
    optimize_cmd.add_argument("--skip-clean", action="store_true", help="Skip cache cleanup stage.")
    optimize_cmd.add_argument("--skip-index", action="store_true", help="Skip index stage.")
    optimize_cmd.add_argument(
        "--allow-audit-warnings",
        action="store_true",
        help="Do not fail optimize stage when audit has warnings.",
    )
    optimize_cmd.add_argument("--out", default="", help="Output optimize report json path.")
    optimize_cmd.add_argument("--audit-out", default="", help="Output audit report json path.")
    optimize_cmd.add_argument("--index-out", default="", help="Output index report json path.")
    optimize_cmd.add_argument(
        "--data",
        nargs="*",
        default=[],
        help="Optional dataset path scope for data-governance audit during optimize.",
    )
    optimize_cmd.set_defaults(func=cmd_optimize)

    gate_c_cmd = sub.add_parser(
        "run-gate-c",
        help="Run Gate A → B → C validation pipeline for a single alpha.",
    )
    gate_c_cmd.add_argument("alpha_id", help="Alpha ID (must exist under research/alphas/)")
    gate_c_cmd.add_argument(
        "--data",
        nargs="+",
        required=True,
        metavar="PATH",
        help="One or more .npy data file paths for backtesting.",
    )
    gate_c_cmd.add_argument("--oos-split", type=float, default=0.7, help="In-sample / OOS split ratio (default 0.7).")
    gate_c_cmd.add_argument(
        "--latency-profile",
        default="shioaji_sim_p95_v2026-03-04",
        help="Latency profile ID from config/research/latency_profiles.yaml.",
    )
    gate_c_cmd.add_argument(
        "--skip-gate-b",
        action="store_true",
        help="Skip Gate B (pytest) — useful when tests were already run separately.",
    )
    gate_c_cmd.add_argument(
        "--opt-threshold-min",
        type=float,
        default=0.01,
        help="Minimum signal threshold for parameter optimization grid (default 0.01).",
    )
    gate_c_cmd.add_argument(
        "--no-opt",
        action="store_true",
        help="Disable parameter optimization (useful when signal has no meaningful threshold).",
    )
    gate_c_cmd.set_defaults(func=cmd_run_gate_c)

    bayesian_cmd = sub.add_parser(
        "run-bayesian-opt",
        help="Run Bayesian optimization (Optuna TPE) for alpha parameter search.",
    )
    bayesian_cmd.add_argument("alpha_id", help="Alpha ID (must exist under research/alphas/)")
    bayesian_cmd.add_argument(
        "--data",
        nargs="+",
        required=True,
        metavar="PATH",
        help="One or more .npy data file paths for backtesting.",
    )
    bayesian_cmd.add_argument("--n-trials", type=int, default=30, help="Number of Optuna trials (default 30).")
    bayesian_cmd.add_argument(
        "--param",
        action="append",
        metavar="name:lo:hi[:log]",
        help="Parameter spec (repeatable). Format: name:lo:hi or name:lo:hi:log.",
    )
    bayesian_cmd.add_argument(
        "--latency-profile",
        default="shioaji_sim_p95_v2026-03-04",
        help="Latency profile ID from config/research/latency_profiles.yaml.",
    )
    bayesian_cmd.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility.")
    bayesian_cmd.add_argument("--oos-split", type=float, default=0.7, help="In-sample / OOS split ratio (default 0.7).")
    bayesian_cmd.set_defaults(func=cmd_run_bayesian_opt)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
