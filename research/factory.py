from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

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
    "combinatorial",
    "data",
    "experiments",
    "knowledge",
    "logs",
    "reports",
    "results_batch6",
    "results_batch7",
    "registry",
    "results",
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
    "data_governance.py",
    "factor_registry.py",
    "feature_benchmark_matrix.py",
    "feature_promotion_check.py",
    "fetch_paper.py",
    "latency_profiles.py",
    "maintenance.py",
    "paper_autofill.py",
    "paper_prototype.py",
    "paper_trade.py",
    "render_promotion_report.py",
    "synth_lob_gen.py",
    "vm_ul.py",
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
            + ". Add skill attribution per SOP Stage 2 (iterative-retrieval, hft-backtester, etc.)."
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
    index_path = ROOT / "knowledge" / "paper_index.json"
    if not index_path.exists():
        warnings.append("Missing research/knowledge/paper_index.json; paper reference mapping cannot be verified.")
        details["unresolved_paper_refs"] = unresolved
        return

    try:
        payload = json.loads(index_path.read_text())
        known_refs = set(str(k) for k in payload.keys())
    except (OSError, ValueError):
        warnings.append("Invalid research/knowledge/paper_index.json; failed to parse paper refs.")
        details["unresolved_paper_refs"] = unresolved
        return

    try:
        from research.registry.alpha_registry import AlphaRegistry

        registry = AlphaRegistry()
        loaded = registry.discover(ROOT / "alphas")
        if registry.errors:
            warnings.append("Alpha registry discovery reported import errors during paper-ref audit.")
            details["registry_errors"] = list(registry.errors)
        for alpha_id, alpha in loaded.items():
            refs = [str(ref) for ref in alpha.manifest.paper_refs]
            unknown = [ref for ref in refs if ref and ref not in known_refs]
            if unknown:
                unresolved[alpha_id] = unknown
    except Exception as exc:
        warnings.append(f"Failed to audit manifest paper_refs: {exc}")

    details["unresolved_paper_refs"] = unresolved
    if unresolved:
        warnings.append("Some manifest paper_refs are not mapped in research/knowledge/paper_index.json.")


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
                hits.append(str(file_path.relative_to(ROOT)))
    details["binary_pollution_in_source_zones"] = sorted(hits)
    if hits:
        warnings.append("Binary artifacts detected in source zones; move to research/data or research/archive.")


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
        errors.append(
            "Data governance violation: metadata sidecar missing for dataset(s): "
            + ", ".join(missing_meta)
        )
    if invalid_meta:
        bad = ", ".join(f"{path}({';'.join(problems)})" for path, problems in sorted(invalid_meta.items()))
        errors.append("Data governance violation: metadata sidecar invalid for dataset(s): " + bad)


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
    from src.hft_platform.alpha.validation import run_gate_a, run_gate_b, run_gate_c

    resolved = _resolve_gate_cli_args(args, "run-gate-c")
    if resolved is None:
        return 1
    alpha_id, data_paths, latency_profile_id, latency, alpha_instance, config, resolved_paths = resolved

    manifest = alpha_instance.manifest
    project_root = ROOT.parent
    skip_gate_b: bool = bool(getattr(args, "skip_gate_b", False))

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


def _resolve_gate_cli_args(
    args: argparse.Namespace,
    label: str,
) -> tuple[str, list[str], str, dict[str, Any], Any, Any, list[str]] | None:
    """Shared setup for run-gate-c and run-gate-all: parse args, load latency,
    discover alpha, resolve data paths.

    Returns (alpha_id, data_paths, latency_profile_id, latency, alpha_instance,
    ValidationConfig, resolved_paths) or None on error (already printed).
    """
    from research.registry.alpha_registry import AlphaRegistry
    from research.tools.latency_profiles import load_latency_profile
    from src.hft_platform.alpha.validation import ValidationConfig

    alpha_id: str = str(args.alpha_id)
    data_paths: list[str] = list(args.data or [])
    oos_split: float = float(args.oos_split)
    latency_profile_id: str = str(args.latency_profile)

    try:
        latency = load_latency_profile(latency_profile_id)
    except (KeyError, FileNotFoundError) as exc:
        print(f"[{label}] ERROR: {exc}")
        return None

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

    registry = AlphaRegistry()
    loaded = registry.discover(ROOT / "alphas")
    if alpha_id not in loaded:
        print(f"[{label}] ERROR: alpha '{alpha_id}' not found in research/alphas/")
        print(f"  Available: {sorted(loaded.keys())}")
        return None

    alpha_instance = loaded[alpha_id]
    project_root = ROOT.parent

    resolved_paths: list[str] = []
    for p in data_paths:
        candidate = Path(p)
        if not candidate.is_absolute():
            candidate = (project_root / p).resolve()
        resolved_paths.append(str(candidate))

    return (alpha_id, data_paths, latency_profile_id, latency, alpha_instance, config, resolved_paths)


def cmd_run_gate_all(args: argparse.Namespace) -> int:
    """Run Gate A -> B -> C -> D -> E sequentially for a single alpha."""
    from src.hft_platform.alpha.promotion import PromotionConfig, _evaluate_gate_d, _evaluate_gate_e
    from src.hft_platform.alpha.validation import run_gate_a, run_gate_b, run_gate_c

    resolved = _resolve_gate_cli_args(args, "run-gate-all")
    if resolved is None:
        return 1
    alpha_id, data_paths, latency_profile_id, latency, alpha_instance, config, resolved_paths = resolved

    manifest = alpha_instance.manifest
    project_root = ROOT.parent
    skip_gate_b: bool = bool(getattr(args, "skip_gate_b", False))
    skip_gate_e: bool = bool(getattr(args, "skip_gate_e", False))
    shadow_sessions: int = int(getattr(args, "shadow_sessions", 5))

    print(f"\n=== Alpha Pipeline: {alpha_id} ===")
    print(f"  latency_profile : {latency_profile_id}")
    print(f"  data_paths      : {resolved_paths}")

    gate_results: dict[str, dict[str, Any]] = {}
    run_id: str | None = None
    failed = False

    # --- Gate A ---
    gate_a = run_gate_a(manifest, resolved_paths, config=config, root=project_root)
    gate_results["Gate A"] = {
        "passed": gate_a.passed,
        "detail": "manifest valid, data governance OK" if gate_a.passed else str(gate_a.details),
    }
    if not gate_a.passed:
        failed = True

    # --- Gate B ---
    if not failed:
        if skip_gate_b:
            gate_results["Gate B"] = {"passed": True, "detail": "SKIPPED (--skip-gate-b)"}
        else:
            gate_b = run_gate_b(alpha_id, project_root)
            detail_b = (
                f"{gate_b.details.get('tests_passed', '?')} tests passed"
                if gate_b.passed
                else str(gate_b.details.get("stderr_tail", ""))
            )
            gate_results["Gate B"] = {"passed": gate_b.passed, "detail": detail_b}
            if not gate_b.passed:
                failed = True

    # --- Gate C ---
    scorecard_path: str | None = None
    if not failed:
        experiments_base = ROOT / "experiments"
        gate_c_result = run_gate_c(alpha_instance, config, project_root, resolved_paths, experiments_base)
        gate_c, run_id, _config_hash, scorecard_path, _experiment_meta_path = gate_c_result
        details_c = gate_c.details
        detail_c = (
            f"Sharpe OOS={details_c.get('sharpe_oos')}, IC={details_c.get('ic_mean')}, drawdown={details_c.get('max_drawdown')}"
            if gate_c.passed
            else str(details_c)
        )
        gate_results["Gate C"] = {"passed": gate_c.passed, "detail": detail_c, "scorecard_path": scorecard_path, "run_id": run_id}
        if not gate_c.passed:
            failed = True

    # --- Gate D ---
    if not failed:
        if not scorecard_path or not Path(scorecard_path).exists():
            gate_results["Gate D"] = {"passed": False, "detail": "scorecard not found after Gate C"}
            failed = True
        else:
            scorecard = json.loads(Path(scorecard_path).read_text())
            promo_config = PromotionConfig(
                alpha_id=alpha_id,
                owner="research-cli",
                project_root=str(project_root),
                scorecard_path=scorecard_path,
                shadow_sessions=shadow_sessions,
                manifest_feature_set_version=getattr(manifest, "feature_set_version", None),
                write_promotion_config=False,
            )
            gate_d_passed, gate_d_checks = _evaluate_gate_d(scorecard, promo_config)
            gate_results["Gate D"] = {
                "passed": gate_d_passed,
                "detail": "meets promotion thresholds" if gate_d_passed else str(gate_d_checks),
                "checks": gate_d_checks,
            }
            if not gate_d_passed:
                failed = True

    # --- Gate E ---
    if not failed:
        if skip_gate_e:
            gate_results["Gate E"] = {"passed": True, "detail": "SKIPPED (--skip-gate-e)"}
        else:
            gate_e_passed, gate_e_checks = _evaluate_gate_e(promo_config, project_root)  # type: ignore[possibly-undefined]
            gate_results["Gate E"] = {
                "passed": gate_e_passed,
                "detail": "shadow sessions OK" if gate_e_passed else str(gate_e_checks),
                "checks": gate_e_checks,
            }
            if not gate_e_passed:
                failed = True

    _print_gate_all_summary(alpha_id, gate_results, skip_gate_e=skip_gate_e)
    _save_gate_all_report(alpha_id, gate_results, run_id)
    return 1 if failed else 0


def _print_gate_all_summary(
    alpha_id: str,
    gate_results: dict[str, dict[str, Any]],
    *,
    skip_gate_e: bool = False,
) -> None:
    """Print a human-readable summary table for all gates."""
    print(f"\n=== Alpha Pipeline: {alpha_id} ===")
    all_gates = ("Gate A", "Gate B", "Gate C", "Gate D", "Gate E")
    all_passed = True
    for gate in all_gates:
        if gate not in gate_results:
            if gate == "Gate E" and skip_gate_e:
                print(f"  {gate}: SKIP  (--skip-gate-e)")
            else:
                print(f"  {gate}: --    (not reached)")
                all_passed = False
            continue
        result = gate_results[gate]
        passed = result["passed"]
        if not passed:
            all_passed = False
        detail_display = str(result.get("detail", ""))[:120]
        status = "PASS" if passed else "FAIL"
        print(f"  {gate}: {status}  ({detail_display})")

    print("\nResult: APPROVED for canary" if all_passed else "\nResult: REJECTED")


def _save_gate_all_report(
    alpha_id: str,
    gate_results: dict[str, dict[str, Any]],
    run_id: str | None,
) -> None:
    """Save full gate results to a JSON report under experiments/runs/."""
    report_run_id = run_id or f"gate_all_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{alpha_id}"
    report_dir = ROOT / "experiments" / "runs" / report_run_id
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "gate_all_report.json"

    payload = {
        "alpha_id": alpha_id,
        "run_id": report_run_id,
        "timestamp": _now_iso(),
        "gates": gate_results,
        "overall_passed": all(r["passed"] for r in gate_results.values()),
    }
    _write_json(report_path, payload)
    print(f"\n[run-gate-all] report saved: {report_path}")


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

    gate_all_cmd = sub.add_parser(
        "run-gate-all",
        help="Run Gate A -> B -> C -> D -> E end-to-end validation pipeline for a single alpha.",
    )
    gate_all_cmd.add_argument("alpha_id", help="Alpha ID (must exist under research/alphas/)")
    gate_all_cmd.add_argument(
        "--data",
        nargs="+",
        required=True,
        metavar="PATH",
        help="One or more .npy data file paths for backtesting.",
    )
    gate_all_cmd.add_argument("--oos-split", type=float, default=0.7, help="In-sample / OOS split ratio (default 0.7).")
    gate_all_cmd.add_argument(
        "--latency-profile",
        default="shioaji_sim_p95_v2026-03-04",
        help="Latency profile ID from config/research/latency_profiles.yaml.",
    )
    gate_all_cmd.add_argument(
        "--skip-gate-b",
        action="store_true",
        help="Skip Gate B (pytest) — useful when tests were already run separately.",
    )
    gate_all_cmd.add_argument(
        "--opt-threshold-min",
        type=float,
        default=0.01,
        help="Minimum signal threshold for parameter optimization grid (default 0.01).",
    )
    gate_all_cmd.add_argument(
        "--no-opt",
        action="store_true",
        help="Disable parameter optimization (useful when signal has no meaningful threshold).",
    )
    gate_all_cmd.add_argument(
        "--skip-gate-e",
        action="store_true",
        help="Skip Gate E (paper trading simulation).",
    )
    gate_all_cmd.add_argument(
        "--shadow-sessions",
        type=int,
        default=5,
        help="Number of simulated paper-trade sessions for Gate E (default 5).",
    )
    gate_all_cmd.set_defaults(func=cmd_run_gate_all)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
