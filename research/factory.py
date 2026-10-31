from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from research.tools.data_governance import validate_metadata_payload

ROOT = Path(__file__).resolve().parent


ALLOWED_ROOT_DIRS: set[str] = {
    ".benchmarks",
    "__pycache__",  # tolerated; clean command removes it
    "alphas",
    "arxiv_paper",
    "arxiv_papers",
    "archive",
    "backtest",
    "calibration",  # research.calibration package (audit.py, cli.py, replay.py …)
    "candidate_loop",  # research.candidate_loop package — governed candidate generation loop (tracked, tested)
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
    "__init__.py",
    "__main__.py",
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
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
            "Move them to research/tools/legacy with `git mv`, or add them to CORE_TOOL_FILES."
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


def _audit_strategy_spec_fixed_templates(errors: list[str], details: dict[str, Any]) -> None:
    """Audit fixed strategy spec templates and existing candidate specs."""
    from hft_platform.alpha.strategy_spec import (
        REQUIRED_TOP_LEVEL_FIELDS,
        load_spec,
        template_field_audit,
        validate_spec,
    )

    template_paths: list[Path] = []
    canonical_template = ROOT / "templates" / "strategy_spec.yaml"
    if canonical_template.exists():
        template_paths.append(canonical_template)
    alpha_templates = ROOT / "alphas" / "_templates"
    if alpha_templates.exists():
        template_paths.extend(sorted(alpha_templates.glob("spec*.yaml")))

    template_records: list[dict[str, Any]] = []
    template_missing_required: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    for template_path in sorted(set(template_paths)):
        rel_path = _rel_to_root(template_path)
        try:
            spec = load_spec(template_path)
        except Exception as exc:  # noqa: BLE001 - audit should collect all failures.
            parse_errors.append({"path": rel_path, "errors": [f"failed to load template: {exc!r}"]})
            continue
        present, missing, extra = template_field_audit(spec)
        template_records.append(
            {
                "path": rel_path,
                "present": present,
                "missing": missing,
                "extra": extra,
            }
        )
        if missing:
            template_missing_required.append({"path": rel_path, "missing": missing})

    candidate_valid: list[str] = []
    candidate_invalid: list[dict[str, Any]] = []
    alphas_root = ROOT / "alphas"
    candidate_paths = (
        sorted(p for p in alphas_root.glob("*/spec.yaml") if not p.parent.name.startswith("_"))
        if alphas_root.exists()
        else []
    )
    for spec_path in candidate_paths:
        rel_path = _rel_to_root(spec_path)
        try:
            spec = load_spec(spec_path)
        except Exception as exc:  # noqa: BLE001 - audit should collect all failures.
            candidate_invalid.append({"path": rel_path, "errors": [f"failed to load spec: {exc!r}"]})
            continue
        spec_errors = validate_spec(spec)
        if spec_errors:
            candidate_invalid.append({"path": rel_path, "errors": spec_errors})
        else:
            candidate_valid.append(rel_path)

    details["strategy_spec_fixed_template_audit"] = {
        "required_top_level_fields": list(REQUIRED_TOP_LEVEL_FIELDS),
        "templates": template_records,
        "template_missing_required": template_missing_required,
        "candidate_valid": candidate_valid,
        "candidate_invalid": candidate_invalid,
        "parse_errors": parse_errors,
    }
    if template_missing_required or parse_errors:
        bad = ", ".join(
            item["path"] for item in [*template_missing_required, *parse_errors]
        )
        errors.append(f"strategy spec template fixed-field audit failed: {bad}")
    if candidate_invalid:
        bad = ", ".join(item["path"] for item in candidate_invalid)
        errors.append(f"candidate strategy spec invalid: {bad}")


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


def _audit_validation_summary_index(details: dict[str, Any]) -> None:
    """Index latest validation-summary artifacts by candidate for replay/search."""
    validations_root = ROOT / "experiments" / "validations"
    summary_paths = sorted(validations_root.glob("**/*_summary.json")) if validations_root.exists() else []
    latest: dict[str, dict[str, Any]] = {}
    parse_errors: list[str] = []

    for summary_path in summary_paths:
        payload = _load_json_object(summary_path)
        rel_path = _rel_to_root(summary_path)
        if payload is None:
            parse_errors.append(rel_path)
            continue
        candidate = str(payload.get("candidate") or "").strip()
        if not candidate:
            continue
        decision = payload.get("research_decision")
        decision_map = decision if isinstance(decision, dict) else {}
        evidence = decision_map.get("evidence")
        evidence_list = [str(item) for item in evidence] if isinstance(evidence, list) else []
        hard_gate = payload.get("hard_gate")
        hard_gate_map = hard_gate if isinstance(hard_gate, dict) else {}
        splits = payload.get("splits")
        splits_map = splits if isinstance(splits, dict) else {}
        full = splits_map.get("full", {})
        full_map = full if isinstance(full, dict) else {}
        out_sample = splits_map.get("out_of_sample", {})
        out_sample_map = out_sample if isinstance(out_sample, dict) else {}
        latest[candidate] = {
            "summary_path": rel_path,
            "research_decision_status": str(decision_map.get("status") or ""),
            "research_decision_reason": str(decision_map.get("reason") or ""),
            "research_decision_evidence": evidence_list,
            "edge_floor_metric": str(payload.get("edge_floor_metric") or ""),
            "mean_net_edge_pts_per_trade": full_map.get("mean_net_edge_pts_per_trade"),
            "edge_floor_cleared": bool(payload.get("edge_floor_cleared")),
            "risk_gate_drawdown_within_2x_average_monthly_net_pnl": hard_gate_map.get(
                "drawdown_within_2x_average_monthly_net_pnl"
            ),
            "full_max_drawdown_net_pts": full_map.get("max_drawdown_net_pts"),
            "full_average_monthly_net_pnl": full_map.get("average_monthly_net_pnl"),
            "full_median_monthly_net_pnl": full_map.get("median_monthly_net_pnl"),
            "full_worst_month_net_pnl": full_map.get("worst_month_net_pnl"),
            "full_max_single_month_net_share_of_positive": full_map.get(
                "max_single_month_net_share_of_positive"
            ),
            "full_drawdown_within_2x_average_monthly_net_pnl": full_map.get(
                "drawdown_within_2x_average_monthly_net_pnl"
            ),
            "out_of_sample_mean_net_edge_pts_per_trade": out_sample_map.get("mean_net_edge_pts_per_trade"),
            "out_of_sample_max_drawdown_net_pts": out_sample_map.get("max_drawdown_net_pts"),
            "out_of_sample_average_monthly_net_pnl": out_sample_map.get("average_monthly_net_pnl"),
            "out_of_sample_median_monthly_net_pnl": out_sample_map.get("median_monthly_net_pnl"),
            "out_of_sample_worst_month_net_pnl": out_sample_map.get("worst_month_net_pnl"),
            "out_of_sample_max_single_month_net_share_of_positive": out_sample_map.get(
                "max_single_month_net_share_of_positive"
            ),
            "out_of_sample_drawdown_within_2x_average_monthly_net_pnl": out_sample_map.get(
                "drawdown_within_2x_average_monthly_net_pnl"
            ),
            "traceability_missing": _validation_summary_missing_fields(payload),
        }

    details["validation_summary_index"] = latest
    details["validation_summary_parse_errors"] = parse_errors


def _audit_research_decision_replay(details: dict[str, Any]) -> None:
    """Create a compact candidate-level decision table from indexed summaries."""
    index = details.get("validation_summary_index")
    index_map = index if isinstance(index, dict) else {}
    comparison = details.get("research_candidate_comparison")
    comparison_map = comparison if isinstance(comparison, dict) else {}
    comparison_rows = comparison_map.get("rows")
    comparison_by_candidate = {
        str(row.get("candidate") or ""): row
        for row in (comparison_rows if isinstance(comparison_rows, list) else [])
        if isinstance(row, dict)
    }
    replay: list[dict[str, Any]] = []
    for candidate, row in sorted(index_map.items()):
        row_map = row if isinstance(row, dict) else {}
        candidate_id = str(candidate)
        comparison_row = comparison_by_candidate.get(candidate_id, {})
        comparison_row_map = comparison_row if isinstance(comparison_row, dict) else {}
        blockers = [str(blocker) for blocker in comparison_row_map.get("blockers", [])]
        traceability_missing = row_map.get("traceability_missing", [])
        traceability_missing_list = [str(field) for field in traceability_missing]
        replay_status = "traceable" if traceability_missing == [] else "legacy_untraceable"
        legacy_blockers = ["legacy_traceability_missing"] if replay_status == "legacy_untraceable" else []
        replay_blockers = blockers or legacy_blockers
        replay.append(
            {
                "candidate": candidate_id,
                "replay_status": replay_status,
                "status": row_map.get("research_decision_status"),
                "reason": row_map.get("research_decision_reason"),
                "summary_path": row_map.get("summary_path"),
                "spec_path": comparison_row_map.get("spec_path", ""),
                "readiness_status": comparison_row_map.get(
                    "eligibility_status",
                    replay_status if legacy_blockers else "",
                ),
                "paper_live_eligible": bool(comparison_row_map.get("paper_live_eligible")),
                "primary_blocker": replay_blockers[0] if replay_blockers else "",
                "blockers": replay_blockers,
                "next_actions": _research_decision_replay_next_actions(
                    blockers=blockers,
                    traceability_missing=traceability_missing_list,
                    comparison_available=bool(comparison_row_map),
                ),
                "command_families": _research_readiness_command_families(replay_blockers),
                "edge_floor_metric": row_map.get("edge_floor_metric"),
                "mean_net_edge_pts_per_trade": row_map.get("mean_net_edge_pts_per_trade"),
                "edge_floor_cleared": row_map.get("edge_floor_cleared"),
                "out_of_sample_mean_net_edge_pts_per_trade": row_map.get(
                    "out_of_sample_mean_net_edge_pts_per_trade"
                ),
                "risk_gate_drawdown_within_2x_average_monthly_net_pnl": row_map.get(
                    "risk_gate_drawdown_within_2x_average_monthly_net_pnl"
                ),
                "full_max_drawdown_net_pts": row_map.get("full_max_drawdown_net_pts"),
                "full_average_monthly_net_pnl": row_map.get("full_average_monthly_net_pnl"),
                "full_worst_month_net_pnl": row_map.get("full_worst_month_net_pnl"),
                "traceability_missing": traceability_missing_list,
            }
        )
    details["research_decision_replay"] = replay


def _research_decision_replay_next_actions(
    *,
    blockers: list[str],
    traceability_missing: list[str],
    comparison_available: bool,
) -> list[str]:
    if comparison_available:
        return _research_readiness_next_actions(blockers)
    if not traceability_missing:
        return []

    actions: list[str] = []
    if any(field in traceability_missing for field in ("artifact_scope", "summary_path")):
        actions.append("backfill_validation_summary_identity_fields")
    if "research_decision" in traceability_missing:
        actions.append("backfill_research_decision_status_reason_evidence")
    if any(
        field in traceability_missing
        for field in ("edge_floor_metric", "splits.full.mean_net_edge_pts_per_trade")
    ):
        actions.append("backfill_round_trip_net_edge_metrics")
    actions.append("exclude_from_paper_live_candidate_comparison_until_backfilled")
    return actions


def _audit_research_record_generation(details: dict[str, Any]) -> None:
    """Project complete research-record rows from specs plus validation summaries."""
    spec_index = _valid_strategy_spec_index()
    summary_index = details.get("validation_summary_index")
    summary_map = summary_index if isinstance(summary_index, dict) else {}
    complete_records: list[dict[str, Any]] = []
    incomplete_records: list[dict[str, Any]] = []
    seen_spec_paths: set[str] = set()

    for candidate, row in sorted(summary_map.items()):
        candidate_id = str(candidate)
        row_map = row if isinstance(row, dict) else {}
        missing = list(row_map.get("traceability_missing") or [])
        spec_record = spec_index.get(candidate_id)
        if spec_record is None:
            incomplete_records.append(
                {
                    "candidate": candidate_id,
                    "summary_path": row_map.get("summary_path"),
                    "missing": [*missing, "strategy_spec"],
                }
            )
            continue
        seen_spec_paths.add(str(spec_record["path"]))
        summary_rel = str(row_map.get("summary_path") or "")
        summary = _load_json_object(ROOT / summary_rel) if summary_rel else None
        if summary is None:
            incomplete_records.append(
                {
                    "candidate": candidate_id,
                    "spec_path": spec_record["path"],
                    "summary_path": summary_rel,
                    "missing": [*missing, "validation_summary_loadable"],
                }
            )
            continue
        if missing:
            incomplete_records.append(
                {
                    "candidate": candidate_id,
                    "spec_path": spec_record["path"],
                    "summary_path": summary_rel,
                    "missing": missing,
                }
            )
            continue

        spec = spec_record["spec"]
        validation_plan = spec.get("validation_plan")
        validation_plan_map = validation_plan if isinstance(validation_plan, dict) else {}
        splits = summary.get("splits")
        split_map = splits if isinstance(splits, dict) else {}
        full = split_map.get("full")
        oos = split_map.get("out_of_sample")
        full_map = full if isinstance(full, dict) else {}
        oos_map = oos if isinstance(oos, dict) else {}
        hard_gate = summary.get("hard_gate")
        hard_gate_map = hard_gate if isinstance(hard_gate, dict) else {}
        definition = summary.get("definition")
        definition_map = definition if isinstance(definition, dict) else {}

        complete_records.append(
            {
                "candidate": candidate_id,
                "spec_path": spec_record["path"],
                "summary_path": summary_rel,
                "strategy_name": spec.get("strategy_name"),
                "market": spec.get("market"),
                "instrument": spec.get("instrument"),
                "hypothesis": spec.get("hypothesis"),
                "timeframe": spec.get("timeframe"),
                "holding_period": spec.get("holding_period"),
                "entry_rule": spec.get("entry_rule"),
                "exit_rule": spec.get("exit_rule"),
                "position_sizing": spec.get("position_sizing"),
                "risk_control": spec.get("risk_control"),
                "cost_assumptions": spec.get("cost_model"),
                "validation_plan": validation_plan,
                "data_range": validation_plan_map.get("data_range"),
                "parameters": definition_map,
                "full_results": _research_record_split_results(full_map),
                "out_of_sample_results": _research_record_split_results(oos_map),
                "risk_metrics": _research_record_risk_metrics(full_map, oos_map, hard_gate_map),
                "research_decision": summary.get("research_decision"),
            }
        )

    for candidate_id, spec_record in _unique_strategy_spec_records(spec_index):
        spec_path = str(spec_record["path"])
        if spec_path in seen_spec_paths:
            continue
        spec = spec_record["spec"]
        incomplete_records.append(
            {
                "candidate": candidate_id,
                "strategy_name": spec.get("strategy_name"),
                "spec_path": spec_path,
                "summary_path": "",
                "missing": ["validation_summary"],
            }
        )

    details["research_record_generation"] = {
        "complete_records": complete_records,
        "incomplete_records": incomplete_records,
    }


def _audit_research_parity_evidence(details: dict[str, Any]) -> None:
    """Audit candidate-level replay/paper/live parity evidence shape."""
    record_audit = details.get("research_record_generation")
    record_map = record_audit if isinstance(record_audit, dict) else {}
    records = record_map.get("complete_records")
    record_list = records if isinstance(records, list) else []
    complete: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for record in record_list:
        if not isinstance(record, dict):
            continue
        candidate = str(record.get("candidate") or "")
        summary_path = str(record.get("summary_path") or "")
        summary = _load_json_object(ROOT / summary_path) if summary_path else None
        parity = summary.get("parity_evidence") if isinstance(summary, dict) else None
        if not isinstance(parity, dict):
            missing.append(
                {
                    "candidate": candidate,
                    "summary_path": summary_path,
                    "status": "missing",
                    "missing": ["parity_evidence"],
                }
            )
            continue

        row = _research_parity_evidence_row(candidate, summary_path, parity)
        if row["status"] == "pass":
            complete.append(row)
        elif row["status"] == "fail":
            failed.append(row)
        else:
            invalid.append(row)

    details["research_parity_evidence"] = {
        "required_checks": list(_RESEARCH_PARITY_REQUIRED_CHECKS),
        "allowed_mismatch_categories": _research_parity_allowed_mismatch_categories(),
        "complete": complete,
        "missing": missing,
        "invalid": invalid,
        "failed": failed,
    }


_RESEARCH_PARITY_REQUIRED_CHECKS: tuple[str, ...] = (
    "signal_trigger_time",
    "direction",
    "position_size",
    "entry",
    "exit",
    "session_filter",
    "risk_filter",
    "force_flat_rule",
)


def _research_parity_allowed_mismatch_categories() -> list[str]:
    from hft_platform.alpha.divergence_category import DivergenceCategory

    return [category.value for category in DivergenceCategory]


def _research_parity_evidence_row(
    candidate: str,
    summary_path: str,
    parity: dict[str, Any],
) -> dict[str, Any]:
    match_pct = parity.get("match_pct")
    threshold = parity.get("threshold", 95.0)
    checked = parity.get("checked_dimensions")
    checked_list = [str(item) for item in checked] if isinstance(checked, list) else []
    mismatch_counts_raw = parity.get("mismatch_counts")
    mismatch_counts = mismatch_counts_raw if isinstance(mismatch_counts_raw, dict) else {}
    allowed = set(_research_parity_allowed_mismatch_categories())
    invalid_categories = sorted(str(category) for category in mismatch_counts if str(category) not in allowed)
    missing_checks = [check for check in _RESEARCH_PARITY_REQUIRED_CHECKS if check not in checked_list]
    errors: list[str] = []
    if parity.get("artifact_scope") != "parity_evidence":
        errors.append("artifact_scope")
    if not isinstance(match_pct, int | float):
        errors.append("match_pct")
    elif isinstance(threshold, int | float) and float(match_pct) < float(threshold):
        errors.append("match_pct_below_threshold")
    if not isinstance(threshold, int | float):
        errors.append("threshold")
    if not isinstance(checked, list):
        errors.append("checked_dimensions")
    elif missing_checks:
        errors.append("missing_required_checks")
    if not isinstance(mismatch_counts_raw, dict):
        errors.append("mismatch_counts")
    elif invalid_categories:
        errors.append("invalid_mismatch_categories")

    status = "pass"
    if errors:
        schema_error_names = {
            "artifact_scope",
            "match_pct",
            "threshold",
            "checked_dimensions",
            "missing_required_checks",
            "mismatch_counts",
            "invalid_mismatch_categories",
        }
        status = "invalid" if any(error in schema_error_names for error in errors) else "fail"
    return {
        "candidate": candidate,
        "summary_path": summary_path,
        "status": status,
        "match_pct": match_pct,
        "threshold": threshold,
        "mismatch_counts": mismatch_counts,
        "invalid_mismatch_categories": invalid_categories,
        "missing_checks": missing_checks,
        "errors": errors,
    }


def _audit_research_candidate_comparison(details: dict[str, Any]) -> None:
    """Build a uniform candidate comparison table from complete research records."""
    record_audit = details.get("research_record_generation")
    record_map = record_audit if isinstance(record_audit, dict) else {}
    records = record_map.get("complete_records")
    record_list = records if isinstance(records, list) else []
    incomplete_records = record_map.get("incomplete_records")
    incomplete_list = incomplete_records if isinstance(incomplete_records, list) else []
    parity_by_candidate = _research_parity_evidence_by_candidate(details)
    rows = [
        _research_candidate_comparison_row(record, parity_by_candidate.get(str(record.get("candidate") or "")))
        for record in record_list
        if isinstance(record, dict)
    ]
    rows.extend(
        _research_incomplete_candidate_comparison_row(record)
        for record in incomplete_list
        if isinstance(record, dict) and "spec_path" in record
    )
    rows.sort(
        key=lambda row: (
            not row["paper_live_eligible"],
            -_sortable_number(row.get("out_of_sample_mean_net_edge_pts_per_trade")),
            -_sortable_number(row.get("mean_net_edge_pts_per_trade")),
            str(row.get("candidate") or ""),
        )
    )
    details["research_candidate_comparison"] = {
        "rows": rows,
        "paper_live_candidates": [row["candidate"] for row in rows if row["paper_live_eligible"]],
        "not_eligible": [row["candidate"] for row in rows if not row["paper_live_eligible"]],
    }


def _audit_research_readiness_summary(details: dict[str, Any]) -> None:
    """Build an operator-facing readiness summary from candidate comparison rows."""
    comparison = details.get("research_candidate_comparison")
    comparison_map = comparison if isinstance(comparison, dict) else {}
    rows = comparison_map.get("rows")
    row_list = rows if isinstance(rows, list) else []
    readiness_rows = [_research_readiness_summary_row(row) for row in row_list if isinstance(row, dict)]
    details["research_readiness_summary"] = {
        "schema": "research.readiness_summary.v1",
        "total_candidates": len(readiness_rows),
        "paper_live_candidates": [row["candidate"] for row in readiness_rows if row["paper_live_eligible"]],
        "counts_by_status": _count_values(row["readiness_status"] for row in readiness_rows),
        "counts_by_blocker": _count_values(
            blocker for row in readiness_rows for blocker in row.get("blockers", [])
        ),
        "command_families_by_blocker": _research_readiness_command_families_by_blocker(
            readiness_rows
        ),
        "rows": readiness_rows,
    }


def _audit_research_candidate_advancement(details: dict[str, Any]) -> None:
    readiness = details.get("research_readiness_summary")
    readiness_map = readiness if isinstance(readiness, dict) else {}
    details["research_candidate_advancement"] = _research_candidate_advancement_payload(readiness_map)


def _research_readiness_summary_row(row: dict[str, Any]) -> dict[str, Any]:
    blockers = [str(blocker) for blocker in row.get("blockers", [])]
    return {
        "candidate": row.get("candidate"),
        "readiness_status": row.get("eligibility_status"),
        "paper_live_eligible": bool(row.get("paper_live_eligible")),
        "primary_blocker": blockers[0] if blockers else "",
        "blockers": blockers,
        "next_actions": _research_readiness_next_actions(blockers),
        "command_families": _research_readiness_command_families(blockers),
        "metrics": {
            "mean_net_edge_pts_per_trade": row.get("mean_net_edge_pts_per_trade"),
            "out_of_sample_mean_net_edge_pts_per_trade": row.get(
                "out_of_sample_mean_net_edge_pts_per_trade"
            ),
            "edge_floor_pts": row.get("edge_floor_pts"),
            "full_events": row.get("full_events"),
            "out_of_sample_trading_days": row.get("out_of_sample_trading_days"),
            "drawdown_within_2x_average_monthly_net_pnl": row.get(
                "drawdown_within_2x_average_monthly_net_pnl"
            ),
            "out_of_sample_pnl_distribution_checked": row.get(
                "out_of_sample_pnl_distribution_checked"
            ),
            "out_of_sample_loss_distribution_checked": row.get(
                "out_of_sample_loss_distribution_checked"
            ),
            "out_of_sample_single_trade_dominance_passed": row.get(
                "out_of_sample_single_trade_dominance_passed"
            ),
            "out_of_sample_single_day_dominance_passed": row.get(
                "out_of_sample_single_day_dominance_passed"
            ),
            "parity_evidence_status": row.get("parity_evidence_status"),
            "replay_match_pct": row.get("replay_match_pct"),
        },
        "summary_path": row.get("summary_path"),
        "spec_path": row.get("spec_path"),
    }


def _research_readiness_next_actions(blockers: list[str]) -> list[str]:
    if not blockers:
        return ["paper_live_validation_ready"]
    actions: list[str] = []
    if "research_decision_failed" in blockers:
        actions.append("retain_failed_research_record")
    if any(blocker.endswith("edge_floor_not_cleared") for blocker in blockers):
        actions.append("stop_or_form_new_hypothesis_after_edge_failure")
    if any(blocker in blockers for blocker in ("min_round_trips_not_met", "min_oos_trading_days_not_met")):
        actions.append("collect_more_sample_before_completion")
    if "drawdown_gate_failed" in blockers:
        actions.append("review_drawdown_monthly_distribution")
    if _has_oos_distribution_blocker(blockers):
        actions.append("review_out_of_sample_distribution_dominance")
    if "validation_plan_promotion_blockers" in blockers:
        actions.append("clear_validation_plan_promotion_blockers")
    if "validation_summary_missing" in blockers:
        actions.append("run_validation_summary_generation_before_readiness")
    if any(blocker.startswith("parity_evidence_") for blocker in blockers):
        actions.append("provide_or_attach_replay_paper_live_parity_evidence")
    return actions or ["inspect_candidate_blockers"]


def _research_readiness_command_families_by_blocker(
    readiness_rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    blockers = {
        str(blocker)
        for row in readiness_rows
        for blocker in row.get("blockers", [])
    }
    return {
        family["blocker"]: {
            "command_family": family["command_family"],
            "attach_target": family["attach_target"],
            "commands": family["commands"],
        }
        for family in _research_readiness_command_families(sorted(blockers))
    }


def _research_readiness_command_families(blockers: list[str]) -> list[dict[str, Any]]:
    blocker_set = set(blockers)
    families: dict[str, dict[str, Any]] = {}
    if "out_of_sample_distribution_evidence_missing" in blocker_set:
        families["out_of_sample_distribution_evidence_missing"] = {
            "blocker": "out_of_sample_distribution_evidence_missing",
            "command_family": "oos_distribution_evidence",
            "attach_target": "validation_summary.splits.out_of_sample",
            "commands": [
                "oos-distribution-evidence-backfill-plan",
                "oos-distribution-evidence-template",
                "oos-distribution-evidence-validate",
                "oos-distribution-evidence-attach",
            ],
        }
    if "parity_evidence_missing" in blocker_set:
        families["parity_evidence_missing"] = {
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
    return [families[blocker] for blocker in blockers if blocker in families]


def _count_values(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


_ADVANCEMENT_STATUS_ROUTE: dict[str, str] = {
    "ready_for_paper": "prepare_paper_candidate",
    "evidence_backfill_candidate": "backfill_evidence",
    "sample_expansion_candidate": "expand_sample",
    "hypothesis_review_candidate": "review_hypothesis",
    "parity_repair_candidate": "repair_parity",
    "artifact_repair_candidate": "repair_artifact_integrity",
    "archive_candidate": "archive_candidate_set",
}


def _research_candidate_advancement_payload(readiness: dict[str, Any]) -> dict[str, Any]:
    rows = readiness.get("rows")
    readiness_rows = rows if isinstance(rows, list) else []
    candidates = [
        _research_candidate_advancement_row(row)
        for row in readiness_rows
        if isinstance(row, dict)
    ]
    route, target_status = _research_candidate_advancement_route(candidates)
    target_group = [
        str(row.get("candidate") or "")
        for row in candidates
        if row["advancement_status"] == target_status
    ]
    return {
        "generated_at": _now_iso(),
        "schema": "research.readiness_candidate_advancement.v1",
        "total_candidates": len(candidates),
        "recommended_research_route": route,
        "recommended_candidate": target_group[0] if target_group else "",
        "recommended_candidate_group": target_group,
        "summary": {
            "counts_by_advancement_status": _count_values(
                row["advancement_status"] for row in candidates
            ),
        },
        "candidates": candidates,
    }


def _research_candidate_advancement_route(candidates: list[dict[str, Any]]) -> tuple[str, str]:
    statuses = [str(row.get("advancement_status") or "") for row in candidates]
    for status in ("ready_for_paper", "evidence_backfill_candidate"):
        if status in statuses:
            return _ADVANCEMENT_STATUS_ROUTE[status], status

    ordered_statuses = (
        "sample_expansion_candidate",
        "hypothesis_review_candidate",
        "parity_repair_candidate",
        "artifact_repair_candidate",
        "archive_candidate",
    )
    counts = {status: statuses.count(status) for status in ordered_statuses}
    target_status = max(
        ordered_statuses,
        key=lambda status: (counts[status], -ordered_statuses.index(status)),
    )
    if counts[target_status] <= 0:
        target_status = "archive_candidate"
    return _ADVANCEMENT_STATUS_ROUTE[target_status], target_status


def _research_candidate_advancement_row(row: dict[str, Any]) -> dict[str, Any]:
    blockers = [str(blocker) for blocker in row.get("blockers", [])]
    metrics_raw = row.get("metrics")
    metrics = metrics_raw if isinstance(metrics_raw, dict) else {}
    status = _research_candidate_advancement_status(
        blockers,
        metrics,
        bool(row.get("paper_live_eligible")),
    )
    return {
        "candidate": row.get("candidate"),
        "advancement_status": status,
        "primary_reason": _research_candidate_advancement_reason(status, blockers),
        "supporting_metrics": metrics,
        "blocking_factors": blockers,
        "evidence_gaps": _research_candidate_evidence_gaps(blockers),
        "risk_flags": _research_candidate_risk_flags(blockers),
        "next_research_action": _research_candidate_next_action(status),
        "owner_action_hint": _research_candidate_owner_hint(status),
        "readiness_status": row.get("readiness_status"),
        "summary_path": row.get("summary_path"),
        "spec_path": row.get("spec_path"),
    }


def _research_candidate_advancement_status(
    blockers: list[str],
    metrics: dict[str, Any],
    paper_live_eligible: bool,
) -> str:
    blocker_set = set(blockers)
    if paper_live_eligible and not blockers:
        return "ready_for_paper"
    if _has_artifact_repair_blocker(blocker_set):
        return "artifact_repair_candidate"
    if _is_archive_candidate(blocker_set):
        return "archive_candidate"
    if _has_parity_repair_blocker(blocker_set):
        return "parity_repair_candidate"
    if _has_evidence_backfill_blocker(blocker_set) and not _has_core_metric_blocker(blocker_set):
        return "evidence_backfill_candidate"
    if _has_sample_expansion_blocker(blocker_set) and _candidate_has_edge_signal(metrics):
        return "sample_expansion_candidate"
    if _has_hypothesis_review_blocker(blocker_set):
        return "hypothesis_review_candidate"
    return "archive_candidate"


def _has_artifact_repair_blocker(blockers: set[str]) -> bool:
    return bool(
        blockers
        & {
            "validation_summary_missing",
            "research_decision_blocked_by_audit",
            "legacy_traceability_missing",
            "parity_evidence_invalid",
        }
    )


def _is_archive_candidate(blockers: set[str]) -> bool:
    core_failures = 0
    if blockers & {"full_edge_floor_not_cleared", "out_of_sample_edge_floor_not_cleared"}:
        core_failures += 1
    if blockers & {"min_round_trips_not_met", "min_oos_trading_days_not_met"}:
        core_failures += 1
    if blockers & {"drawdown_gate_failed"}:
        core_failures += 1
    if blockers & {"research_decision_failed"}:
        core_failures += 1
    return core_failures >= 3


def _has_parity_repair_blocker(blockers: set[str]) -> bool:
    return any(
        blocker.startswith("parity_evidence_") and blocker != "parity_evidence_missing"
        for blocker in blockers
    )


def _has_evidence_backfill_blocker(blockers: set[str]) -> bool:
    return bool(blockers & {"parity_evidence_missing", "out_of_sample_distribution_evidence_missing"})


def _has_core_metric_blocker(blockers: set[str]) -> bool:
    return bool(
        blockers
        & {
            "full_edge_floor_not_cleared",
            "out_of_sample_edge_floor_not_cleared",
            "min_round_trips_not_met",
            "min_oos_trading_days_not_met",
            "drawdown_gate_failed",
        }
    )


def _has_sample_expansion_blocker(blockers: set[str]) -> bool:
    return bool(blockers & {"min_round_trips_not_met", "min_oos_trading_days_not_met"})


def _candidate_has_edge_signal(metrics: dict[str, Any]) -> bool:
    edge_floor = metrics.get("edge_floor_pts")
    threshold = float(edge_floor) if isinstance(edge_floor, int | float) else 10.0
    edges = (
        metrics.get("mean_net_edge_pts_per_trade"),
        metrics.get("out_of_sample_mean_net_edge_pts_per_trade"),
    )
    return any(isinstance(edge, int | float) and float(edge) > threshold for edge in edges)


def _has_hypothesis_review_blocker(blockers: set[str]) -> bool:
    return bool(
        blockers
        & {
            "full_edge_floor_not_cleared",
            "out_of_sample_edge_floor_not_cleared",
            "drawdown_gate_failed",
            "out_of_sample_pnl_distribution_not_checked",
            "out_of_sample_loss_distribution_not_checked",
            "out_of_sample_single_trade_dominance_failed",
            "out_of_sample_single_day_dominance_failed",
            "research_decision_needs_more_sample",
            "research_decision_inconclusive",
            "research_decision_blocked_by_risk",
        }
    )


def _research_candidate_evidence_gaps(blockers: list[str]) -> list[str]:
    gaps: list[str] = []
    if "parity_evidence_missing" in blockers:
        gaps.append("parity_evidence")
    if "out_of_sample_distribution_evidence_missing" in blockers:
        gaps.append("out_of_sample_distribution_evidence")
    if "validation_summary_missing" in blockers:
        gaps.append("validation_summary")
    if any(blocker.endswith("_not_checked") for blocker in blockers):
        gaps.append("checked_distribution_fields")
    return gaps


def _research_candidate_risk_flags(blockers: list[str]) -> list[str]:
    flags: list[str] = []
    if "drawdown_gate_failed" in blockers:
        flags.append("drawdown_risk")
    if any("dominance_failed" in blocker for blocker in blockers):
        flags.append("pnl_concentration_risk")
    if _has_parity_repair_blocker(set(blockers)):
        flags.append("parity_drift")
    if "research_decision_failed" in blockers:
        flags.append("failed_research_decision")
    return flags


def _research_candidate_advancement_reason(status: str, blockers: list[str]) -> str:
    reasons = {
        "ready_for_paper": "edge_sample_drawdown_oos_parity_and_promotion_readiness_passed",
        "evidence_backfill_candidate": "core_metrics_pass_but_required_evidence_artifact_missing",
        "sample_expansion_candidate": "edge_signal_present_but_sample_or_oos_days_below_target",
        "hypothesis_review_candidate": "edge_or_pnl_path_quality_does_not_clear_research_bar",
        "parity_repair_candidate": "replay_paper_live_parity_drift_or_failed_parity_evidence",
        "artifact_repair_candidate": "metrics_schema_or_validation_artifact_gap_blocks_decision",
        "archive_candidate": "multiple_core_conditions_failed_without_clear_repair_path",
    }
    return reasons.get(status) or (blockers[0] if blockers else "classified")


def _research_candidate_next_action(status: str) -> str:
    actions = {
        "ready_for_paper": "prepare_paper_candidate",
        "evidence_backfill_candidate": "backfill_missing_evidence_artifacts",
        "sample_expansion_candidate": "expand_sample_and_rerun_validation",
        "hypothesis_review_candidate": "review_or_reformulate_hypothesis",
        "parity_repair_candidate": "repair_replay_paper_live_parity",
        "artifact_repair_candidate": "repair_artifact_schema_or_metrics_projection",
        "archive_candidate": "archive_or_reject_candidate_set",
    }
    return actions[status]


def _research_candidate_owner_hint(status: str) -> str:
    hints = {
        "ready_for_paper": "draft paper-trade preparation packet for the candidate",
        "evidence_backfill_candidate": "attach OOS distribution or parity evidence before reranking",
        "sample_expansion_candidate": "collect more trading days or round trips without relaxing edge gates",
        "hypothesis_review_candidate": "inspect edge source, cost-adjusted PnL path, and concentration",
        "parity_repair_candidate": (
            "compare trigger time, direction, size, entry/exit, session, risk, and position filters"
        ),
        "artifact_repair_candidate": "restore validation summary, schema, metrics, or traceability fields",
        "archive_candidate": "record rejection rationale and remove from active advancement queue",
    }
    return hints[status]


def _research_parity_evidence_by_candidate(details: dict[str, Any]) -> dict[str, dict[str, Any]]:
    parity_audit = details.get("research_parity_evidence")
    parity_map = parity_audit if isinstance(parity_audit, dict) else {}
    by_candidate: dict[str, dict[str, Any]] = {}
    for bucket in ("complete", "failed", "invalid", "missing"):
        rows = parity_map.get(bucket)
        row_list = rows if isinstance(rows, list) else []
        for row in row_list:
            if isinstance(row, dict):
                by_candidate[str(row.get("candidate") or "")] = row
    return by_candidate


def _research_candidate_comparison_row(
    record: dict[str, Any],
    parity_evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    validation_plan = record.get("validation_plan")
    validation_plan_map = validation_plan if isinstance(validation_plan, dict) else {}
    sample_targets = validation_plan_map.get("sample_targets")
    sample_targets_map = sample_targets if isinstance(sample_targets, dict) else {}
    full_results = record.get("full_results")
    full_map = full_results if isinstance(full_results, dict) else {}
    oos_results = record.get("out_of_sample_results")
    oos_map = oos_results if isinstance(oos_results, dict) else {}
    risk_metrics = record.get("risk_metrics")
    risk_map = risk_metrics if isinstance(risk_metrics, dict) else {}
    decision = record.get("research_decision")
    decision_map = decision if isinstance(decision, dict) else {}

    edge_floor = float(validation_plan_map.get("net_edge_floor_pts", 10.0))
    full_edge = full_map.get("mean_net_edge_pts_per_trade")
    oos_edge = oos_map.get("mean_net_edge_pts_per_trade")
    full_events = full_map.get("events")
    oos_days = oos_map.get("trading_days")
    oos_pnl_distribution_checked = oos_map.get("pnl_distribution_checked")
    oos_loss_distribution_checked = oos_map.get("loss_distribution_checked")
    oos_single_trade_dominance_passed = oos_map.get("single_trade_dominance_passed")
    oos_single_day_dominance_passed = oos_map.get("single_day_dominance_passed")
    min_round_trips = sample_targets_map.get("min_round_trips")
    min_oos_days = sample_targets_map.get("min_oos_trading_days")
    drawdown_gate = risk_map.get("drawdown_within_2x_average_monthly_net_pnl")
    decision_status = str(decision_map.get("status") or "")
    parity_status = str(parity_evidence.get("status") if parity_evidence else "missing")
    replay_match_pct = parity_evidence.get("match_pct") if parity_evidence else None
    blockers = _research_candidate_blockers(
        full_edge=full_edge,
        oos_edge=oos_edge,
        edge_floor=edge_floor,
        full_events=full_events,
        min_round_trips=min_round_trips,
        oos_days=oos_days,
        min_oos_days=min_oos_days,
        drawdown_gate=drawdown_gate,
        oos_pnl_distribution_checked=oos_pnl_distribution_checked,
        oos_loss_distribution_checked=oos_loss_distribution_checked,
        oos_single_trade_dominance_passed=oos_single_trade_dominance_passed,
        oos_single_day_dominance_passed=oos_single_day_dominance_passed,
        decision_status=decision_status,
        promotion_blockers=validation_plan_map.get("promotion_blockers"),
        parity_status=parity_status,
    )
    paper_live_eligible = not blockers
    return {
        "candidate": record.get("candidate"),
        "strategy_name": record.get("strategy_name"),
        "market": record.get("market"),
        "instrument": record.get("instrument"),
        "timeframe": record.get("timeframe"),
        "holding_period": record.get("holding_period"),
        "data_range": record.get("data_range"),
        "spec_path": record.get("spec_path"),
        "summary_path": record.get("summary_path"),
        "mean_net_edge_pts_per_trade": full_edge,
        "out_of_sample_mean_net_edge_pts_per_trade": oos_edge,
        "edge_floor_pts": edge_floor,
        "full_events": full_events,
        "out_of_sample_trading_days": oos_days,
        "min_round_trips": min_round_trips,
        "min_oos_trading_days": min_oos_days,
        "drawdown_within_2x_average_monthly_net_pnl": drawdown_gate,
        "out_of_sample_pnl_distribution_checked": oos_pnl_distribution_checked,
        "out_of_sample_loss_distribution_checked": oos_loss_distribution_checked,
        "out_of_sample_single_trade_dominance_passed": oos_single_trade_dominance_passed,
        "out_of_sample_single_day_dominance_passed": oos_single_day_dominance_passed,
        "replay_match_pct": replay_match_pct,
        "parity_evidence_status": parity_status,
        "research_decision_status": decision_status,
        "research_decision_reason": str(decision_map.get("reason") or ""),
        "paper_live_eligible": paper_live_eligible,
        "eligibility_status": (
            "paper_live_candidate"
            if paper_live_eligible
            else _research_candidate_eligibility_status(blockers, decision_status)
        ),
        "blockers": blockers,
    }


def _research_incomplete_candidate_comparison_row(record: dict[str, Any]) -> dict[str, Any]:
    spec_path = str(record.get("spec_path") or "")
    spec = _load_strategy_spec_record(spec_path)
    validation_plan = spec.get("validation_plan")
    validation_plan_map = validation_plan if isinstance(validation_plan, dict) else {}
    sample_targets = validation_plan_map.get("sample_targets")
    sample_targets_map = sample_targets if isinstance(sample_targets, dict) else {}
    edge_floor = float(validation_plan_map.get("net_edge_floor_pts", 10.0))
    blockers = ["validation_summary_missing", "research_decision_blocked_by_audit", "parity_evidence_missing"]
    return {
        "candidate": record.get("candidate"),
        "strategy_name": record.get("strategy_name") or spec.get("strategy_name"),
        "market": spec.get("market"),
        "instrument": spec.get("instrument"),
        "timeframe": spec.get("timeframe"),
        "holding_period": spec.get("holding_period"),
        "data_range": validation_plan_map.get("data_range"),
        "spec_path": spec_path,
        "summary_path": str(record.get("summary_path") or ""),
        "mean_net_edge_pts_per_trade": None,
        "out_of_sample_mean_net_edge_pts_per_trade": None,
        "edge_floor_pts": edge_floor,
        "full_events": None,
        "out_of_sample_trading_days": None,
        "min_round_trips": sample_targets_map.get("min_round_trips"),
        "min_oos_trading_days": sample_targets_map.get("min_oos_trading_days"),
        "drawdown_within_2x_average_monthly_net_pnl": None,
        "replay_match_pct": None,
        "parity_evidence_status": "missing",
        "research_decision_status": "blocked_by_audit",
        "research_decision_reason": "missing_validation_summary",
        "paper_live_eligible": False,
        "eligibility_status": "blocked_by_audit",
        "blockers": blockers,
    }


def _research_candidate_blockers(
    *,
    full_edge: Any,
    oos_edge: Any,
    edge_floor: float,
    full_events: Any,
    min_round_trips: Any,
    oos_days: Any,
    min_oos_days: Any,
    drawdown_gate: Any,
    oos_pnl_distribution_checked: Any,
    oos_loss_distribution_checked: Any,
    oos_single_trade_dominance_passed: Any,
    oos_single_day_dominance_passed: Any,
    decision_status: str,
    promotion_blockers: Any,
    parity_status: str,
) -> list[str]:
    blockers: list[str] = []
    if not _metric_gt(full_edge, edge_floor):
        blockers.append("full_edge_floor_not_cleared")
    if not _metric_gt(oos_edge, edge_floor):
        blockers.append("out_of_sample_edge_floor_not_cleared")
    if not _metric_at_least(full_events, min_round_trips):
        blockers.append("min_round_trips_not_met")
    if not _metric_at_least(oos_days, min_oos_days):
        blockers.append("min_oos_trading_days_not_met")
    # Fail closed: the drawdown gate must be *explicitly* True to clear.  A
    # missing gate (None / absent from the validation summary) is unproven
    # drawdown control, not implicit agreement, so it blocks paper/live
    # eligibility exactly like an outright failure.
    if drawdown_gate is not True:
        blockers.append("drawdown_gate_failed")
    blockers.extend(
        _research_oos_distribution_blockers(
            pnl_distribution_checked=oos_pnl_distribution_checked,
            loss_distribution_checked=oos_loss_distribution_checked,
            single_trade_dominance_passed=oos_single_trade_dominance_passed,
            single_day_dominance_passed=oos_single_day_dominance_passed,
        )
    )
    if decision_status in {"failed", "needs_more_sample", "inconclusive", "blocked_by_risk", "blocked_by_audit"}:
        blockers.append(f"research_decision_{decision_status}")
    if isinstance(promotion_blockers, list) and promotion_blockers:
        blockers.append("validation_plan_promotion_blockers")
    if parity_status == "missing":
        blockers.append("parity_evidence_missing")
    elif parity_status != "pass":
        blockers.append(f"parity_evidence_{parity_status}")
    return blockers


def _has_oos_distribution_blocker(blockers: list[str]) -> bool:
    return any(
        blocker
        in {
            "out_of_sample_distribution_evidence_missing",
            "out_of_sample_pnl_distribution_not_checked",
            "out_of_sample_loss_distribution_not_checked",
            "out_of_sample_single_trade_dominance_failed",
            "out_of_sample_single_day_dominance_failed",
        }
        for blocker in blockers
    )


def _research_oos_distribution_blockers(
    *,
    pnl_distribution_checked: Any,
    loss_distribution_checked: Any,
    single_trade_dominance_passed: Any,
    single_day_dominance_passed: Any,
) -> list[str]:
    values = (
        pnl_distribution_checked,
        loss_distribution_checked,
        single_trade_dominance_passed,
        single_day_dominance_passed,
    )
    if any(not isinstance(value, bool) for value in values):
        return ["out_of_sample_distribution_evidence_missing"]
    blockers: list[str] = []
    if not pnl_distribution_checked:
        blockers.append("out_of_sample_pnl_distribution_not_checked")
    if not loss_distribution_checked:
        blockers.append("out_of_sample_loss_distribution_not_checked")
    if not single_trade_dominance_passed:
        blockers.append("out_of_sample_single_trade_dominance_failed")
    if not single_day_dominance_passed:
        blockers.append("out_of_sample_single_day_dominance_failed")
    return blockers


def _research_candidate_eligibility_status(blockers: list[str], decision_status: str) -> str:
    if decision_status in {"failed", "needs_more_sample", "inconclusive", "blocked_by_risk", "blocked_by_audit"}:
        return decision_status
    if any(blocker in blockers for blocker in ("min_round_trips_not_met", "min_oos_trading_days_not_met")):
        return "needs_more_sample"
    if "drawdown_gate_failed" in blockers:
        return "blocked_by_risk"
    if _has_oos_distribution_blocker(blockers):
        return "blocked_by_audit"
    if any(blocker.startswith("parity_evidence_") for blocker in blockers):
        return "blocked_by_parity"
    if "validation_plan_promotion_blockers" in blockers:
        return "blocked_by_audit"
    return "not_eligible"


def _metric_gt(value: Any, threshold: float) -> bool:
    if not isinstance(value, int | float):
        return False
    return float(value) > threshold


def _metric_at_least(value: Any, threshold: Any) -> bool:
    if threshold is None:
        return True
    if not isinstance(value, int | float) or not isinstance(threshold, int | float):
        return False
    return float(value) >= float(threshold)


def _sortable_number(value: Any) -> float:
    if isinstance(value, int | float):
        return float(value)
    return float("-inf")


def _valid_strategy_spec_index() -> dict[str, dict[str, Any]]:
    from hft_platform.alpha.strategy_spec import load_spec, validate_spec

    spec_index: dict[str, dict[str, Any]] = {}
    alphas_root = ROOT / "alphas"
    if not alphas_root.exists():
        return spec_index
    for spec_path in sorted(p for p in alphas_root.glob("*/spec.yaml") if not p.parent.name.startswith("_")):
        try:
            spec = load_spec(spec_path)
        except Exception:  # noqa: BLE001 - audit classification is non-failing here.
            continue
        if validate_spec(spec):
            continue
        record = {"path": _rel_to_root(spec_path), "spec": spec}
        spec_index.setdefault(spec_path.parent.name, record)
        strategy_name = str(spec.get("strategy_name") or "").strip()
        if strategy_name:
            spec_index.setdefault(strategy_name, record)
    return spec_index


def _unique_strategy_spec_records(
    spec_index: dict[str, dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    unique: list[tuple[str, dict[str, Any]]] = []
    seen_paths: set[str] = set()
    for key, record in sorted(spec_index.items()):
        path = str(record.get("path") or "")
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        unique.append((Path(path).parent.name or str(key), record))
    return unique


def _load_strategy_spec_record(spec_path: str) -> dict[str, Any]:
    if not spec_path:
        return {}
    path = ROOT / spec_path
    if not path.exists():
        return {}
    try:
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - audit row stays blocked if spec cannot be loaded.
        return {}
    return spec if isinstance(spec, dict) else {}


def _research_record_split_results(split: dict[str, Any]) -> dict[str, Any]:
    results = {
        "events": split.get("events"),
        "trading_days": split.get("trading_days"),
        "mean_net_edge_pts_per_trade": split.get("mean_net_edge_pts_per_trade"),
        "pnl_distribution_checked": split.get("pnl_distribution_checked"),
        "loss_distribution_checked": split.get("loss_distribution_checked"),
        "single_trade_dominance_passed": split.get("single_trade_dominance_passed"),
        "single_day_dominance_passed": split.get("single_day_dominance_passed"),
    }
    return {key: value for key, value in results.items() if value is not None}


def _research_record_risk_metrics(
    full: dict[str, Any],
    oos: dict[str, Any],
    hard_gate: dict[str, Any],
) -> dict[str, Any]:
    metrics = {
        "full_max_drawdown_net_pts": full.get("max_drawdown_net_pts"),
        "full_average_monthly_net_pnl": full.get("average_monthly_net_pnl"),
        "full_median_monthly_net_pnl": full.get("median_monthly_net_pnl"),
        "full_worst_month_net_pnl": full.get("worst_month_net_pnl"),
        "full_drawdown_within_2x_average_monthly_net_pnl": full.get(
            "drawdown_within_2x_average_monthly_net_pnl"
        ),
        "out_of_sample_max_drawdown_net_pts": oos.get("max_drawdown_net_pts"),
        "out_of_sample_average_monthly_net_pnl": oos.get("average_monthly_net_pnl"),
        "out_of_sample_median_monthly_net_pnl": oos.get("median_monthly_net_pnl"),
        "out_of_sample_worst_month_net_pnl": oos.get("worst_month_net_pnl"),
        "out_of_sample_drawdown_within_2x_average_monthly_net_pnl": oos.get(
            "drawdown_within_2x_average_monthly_net_pnl"
        ),
        "drawdown_within_2x_average_monthly_net_pnl": hard_gate.get(
            "drawdown_within_2x_average_monthly_net_pnl"
        ),
    }
    return {key: value for key, value in metrics.items() if value is not None}


def _validation_summary_missing_fields(payload: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if payload.get("artifact_scope") != "validation_summary":
        missing.append("artifact_scope")
    if not str(payload.get("summary_path") or "").strip():
        missing.append("summary_path")
    decision = payload.get("research_decision")
    if not isinstance(decision, dict):
        missing.append("research_decision")
    else:
        if not str(decision.get("status") or "").strip():
            missing.append("research_decision.status")
        if not str(decision.get("reason") or "").strip():
            missing.append("research_decision.reason")
    if not str(payload.get("edge_floor_metric") or "").strip():
        missing.append("edge_floor_metric")
    splits = payload.get("splits")
    full = splits.get("full") if isinstance(splits, dict) else None
    if not isinstance(full, dict) or full.get("mean_net_edge_pts_per_trade") is None:
        missing.append("splits.full.mean_net_edge_pts_per_trade")
    return missing


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
    _audit_strategy_spec_fixed_templates(errors, details)
    _audit_experiment_edge_metric_semantics(details)
    _audit_experiment_research_decisions(details)
    _audit_validation_summary_index(details)
    _audit_research_record_generation(details)
    _audit_research_parity_evidence(details)
    _audit_research_candidate_comparison(details)
    _audit_research_decision_replay(details)
    _audit_research_readiness_summary(details)
    _audit_research_candidate_advancement(details)

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


def _load_experiment_stats(evidence_root: Path, alpha_ids: Iterable[str] = ()) -> dict[str, Any]:
    from research.registry.legacy_evidence import combined_run_stats, known_alpha_ids

    known = known_alpha_ids(ROOT, extra=alpha_ids)
    return combined_run_stats(evidence_root, ROOT / "experiments" / "runs", known)


def cmd_index(args: argparse.Namespace) -> int:
    from hft_platform.alpha.evidence import EvidenceRootError, evidence_root
    from hft_platform.alpha.status import effective_status, ledger_events_by_alpha
    from research.registry.alpha_registry import AlphaRegistry

    try:
        root = evidence_root()
        ledger = ledger_events_by_alpha(root)
    except (EvidenceRootError, ValueError, OSError) as exc:
        print(f"[research.factory] index: cannot read the verdict ledger: {exc}", file=sys.stderr)
        return 2
    registry = AlphaRegistry()
    loaded = registry.discover(ROOT / "alphas")
    manifests = []
    for alpha_id in sorted(loaded):
        manifest = loaded[alpha_id].manifest
        status = effective_status(manifest.status, ledger.get(manifest.alpha_id, ()))
        manifests.append(
            {
                "alpha_id": manifest.alpha_id,
                "status": status.status.value,
                "status_source": status.source.value,
                "declared_status": manifest.status.value,
                "tier": manifest.tier.value if manifest.tier else None,
                "complexity": manifest.complexity,
                "paper_refs": list(manifest.paper_refs),
                "data_fields": list(manifest.data_fields),
                "rust_module": manifest.rust_module,
                "latency_profile": manifest.latency_profile,
            }
        )

    exp_stats = _load_experiment_stats(root, [item["alpha_id"] for item in manifests])
    for item in manifests:
        stats = exp_stats.get(item["alpha_id"], {})
        item["run_count"] = int(stats.get("run_count", 0))
        item["legacy_run_count"] = int(stats.get("legacy_run_count", 0))
        item["governed_run_count"] = int(stats.get("governed_run_count", 0))
        item["quarantined_run_count"] = int(stats.get("quarantined_run_count", 0))
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only research audit and alpha pipeline index.")
    sub = parser.add_subparsers(dest="command", required=True)

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

    index_cmd = sub.add_parser("index", help="Build machine-readable alpha pipeline index.")
    index_cmd.add_argument("--out", default="", help="Output json path.")
    index_cmd.set_defaults(func=cmd_index)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
