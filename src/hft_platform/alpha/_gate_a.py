from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from hft_platform.alpha._validation_helpers import (
    _dataset_metadata_candidates,
    _dataset_row_count,
    _missing_or_blank_metadata_keys,
    _path_under_any,
    _resolve_allowed_data_roots,
)
from hft_platform.alpha._validation_types import GateReport, ValidationConfig

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "bid_px": ("best_bid", "bid_price", "bid"),
    "ask_px": ("best_ask", "ask_price", "ask"),
    "bid_qty": ("bid_depth", "bid_size", "bqty"),
    "ask_qty": ("ask_depth", "ask_size", "aqty"),
    "trade_vol": ("qty", "volume", "trade_qty"),
    "current_mid": ("mid", "mid_price", "price", "close"),
    "bids": ("lob_bids", "bid_levels", "bid_book"),
    "asks": ("lob_asks", "ask_levels", "ask_book"),
}


def run_gate_a(
    manifest: Any,
    data_paths: list[str],
    *,
    config: ValidationConfig | None = None,
    root: Path | None = None,
) -> GateReport:
    required = [str(field) for field in getattr(manifest, "data_fields", ())]
    available_fields_union: set[str] = set()
    missing_fields_by_path: dict[str, list[str]] = {}
    invalid_data_formats: dict[str, list[str]] = {}

    for path in data_paths:
        data_fields = _load_data_fields(path)
        available_fields_union.update(data_fields)
        missing = [field for field in required if not _field_available(field, data_fields)]
        if missing:
            missing_fields_by_path[path] = missing

        # V2 AOS format validation — only blocking when data governance is enforced
        enforce_dg = bool(config.enforce_data_governance) if config is not None else False
        backtest_engine = str(getattr(config, "backtest_engine", "")) if config is not None else ""
        if enforce_dg or backtest_engine == "hftbacktest_v2":
            fmt_errors = _check_hftbacktest_v2_data_format(path)
            if fmt_errors:
                invalid_data_formats[path] = fmt_errors

    if not data_paths and required:
        missing_fields_by_path["<no_data_paths>"] = list(required)

    missing_fields = sorted({field for fields in missing_fields_by_path.values() for field in fields})

    raw_complexity = str(getattr(manifest, "complexity", ""))
    complexity = raw_complexity.replace(" ", "").upper()
    complexity_ok = complexity in {"O(1)", "O(N)", "ON", "O1"}

    precision_warnings: list[str] = []
    for field in required:
        lower = field.lower()
        if "price" in lower and all(tag not in lower for tag in ("diff", "delta", "return", "spread", "mid")):
            precision_warnings.append(
                f"Field '{field}' may be raw price; ensure scaled-int processing in runtime path."
            )

    alpha_id = str(getattr(manifest, "alpha_id", "")).strip()
    paper_refs = [str(ref).strip() for ref in getattr(manifest, "paper_refs", ()) if str(ref).strip()]
    require_paper_refs = bool(config.require_paper_refs) if config is not None else False
    require_paper_index_link = bool(config.require_paper_index_link) if config is not None else False
    paper_ref_missing = bool(require_paper_refs and not paper_refs)
    unresolved_paper_refs: list[str] = []
    unmapped_paper_refs: list[str] = []
    if require_paper_index_link and paper_refs:
        paper_index = _load_paper_index(root)
        for ref in paper_refs:
            resolved_ref, row = _resolve_paper_ref(ref, paper_index)
            if resolved_ref is None or row is None:
                unresolved_paper_refs.append(ref)
                continue
            mapped = row.get("alphas") if isinstance(row, dict) else None
            mapped_set = {str(x) for x in mapped} if isinstance(mapped, (list, tuple)) else set()
            if alpha_id and alpha_id not in mapped_set:
                unmapped_paper_refs.append(ref)
    paper_governance_passed = not paper_ref_missing and not unresolved_paper_refs and not unmapped_paper_refs

    enforce_data_governance = bool(config.enforce_data_governance) if config is not None else False
    require_data_meta = bool(config.require_data_meta) if config is not None else False
    invalid_data_roots: list[str] = []
    missing_data_metadata: dict[str, str] = {}
    invalid_data_metadata: dict[str, list[str]] = {}
    data_ul_target = int(getattr(config, "data_ul", 2)) if config is not None else 2
    data_ul_target = max(1, min(6, data_ul_target))
    data_ul_achieved_by_path: dict[str, int] = {}
    data_ul_missing_fields_by_path: dict[str, list[str]] = {}
    data_ul_warnings: list[str] = []
    required_data_provenance_fields = (
        tuple(str(x).strip() for x in getattr(config, "required_data_provenance_fields", ()) if str(x).strip())
        if config is not None
        else ()
    )
    allowed_roots: list[str] = []
    if enforce_data_governance:
        allowed_roots = _resolve_allowed_data_roots(root, config)
        for path_str in data_paths:
            data_path = Path(path_str).resolve()
            if allowed_roots and not _path_under_any(data_path, [Path(p) for p in allowed_roots]):
                invalid_data_roots.append(str(data_path))
            if require_data_meta:
                meta_payload, meta_path, meta_error = _load_dataset_metadata(data_path)
                if meta_payload is None:
                    reason = meta_error or "missing"
                    if meta_path is not None:
                        reason = f"{reason} ({meta_path})"
                    missing_data_metadata[str(data_path)] = reason
                    continue
                problems = _validate_dataset_metadata(meta_payload, data_path)
                try:
                    from research.tools.vm_ul import DataUL, infer_data_ul, validate_meta_ul

                    ul_enum = DataUL(data_ul_target)
                    ul_ok, ul_missing = validate_meta_ul(meta_payload, ul_enum)
                    data_ul_achieved = int(infer_data_ul(meta_payload))
                    data_ul_achieved_by_path[str(data_path)] = data_ul_achieved
                    if not ul_ok:
                        data_ul_missing_fields_by_path[str(data_path)] = ul_missing
                        data_ul_warnings.append(
                            f"{data_path.name}: missing UL{data_ul_target} fields: {', '.join(ul_missing)}"
                        )
                except Exception as exc:
                    data_ul_warnings.append(f"data_ul_validation_error:{type(exc).__name__}:{exc}")

                if required_data_provenance_fields and data_ul_target < 2:
                    missing = _missing_or_blank_metadata_keys(meta_payload, required_data_provenance_fields)
                    problems.extend(f"missing_provenance:{key}" for key in missing)
                if problems:
                    invalid_data_metadata[str(data_path)] = problems

    data_governance_passed = (not enforce_data_governance) or (
        not invalid_data_roots and (not require_data_meta or (not missing_data_metadata and not invalid_data_metadata))
    )
    # Note: invalid_data_formats is advisory in Gate A (reported in details).
    # V2 format enforcement is handled at Gate C by HftNativeRunner/ensure_hftbt_npz.

    # Skills / roles attribution governance (warn-only, non-blocking).
    roles_used = list(str(r) for r in getattr(manifest, "roles_used", ()))
    skills_used = list(str(s) for s in getattr(manifest, "skills_used", ()))
    skills_warnings: list[str] = []
    if not skills_used:
        skills_warnings.append(
            "manifest.skills_used is empty — add skill attribution per SOP Stage 2"
            " (e.g. iterative-retrieval, hft-backtest-engine)"
        )
    if not roles_used:
        skills_warnings.append(
            "manifest.roles_used is empty — add role attribution per SOP Stage 2 (e.g. planner, code-reviewer)"
        )
    try:
        from research.registry.schemas import VALID_ROLES, VALID_SKILLS

        invalid_roles_list = [r for r in roles_used if r not in VALID_ROLES]
        invalid_skills_list = [s for s in skills_used if s not in VALID_SKILLS]
    except ImportError:
        invalid_roles_list = []
        invalid_skills_list = []
    if invalid_roles_list:
        skills_warnings.append(
            f"manifest.roles_used contains unknown values: {invalid_roles_list} "
            f"(valid roles are defined in research.registry.schemas.VALID_ROLES)"
        )
    if invalid_skills_list:
        skills_warnings.append(
            f"manifest.skills_used contains unknown values: {invalid_skills_list} "
            f"(valid skills are defined in research.registry.schemas.VALID_SKILLS)"
        )

    # Latency profile advisory check
    enforce_latency = bool(config.enforce_latency_profile) if config is not None else False
    latency_warnings: list[str] = []
    latency_profile_present = False
    latency_profile_valid = False

    manifest_latency = getattr(manifest, "latency_profile", None)
    if not manifest_latency:
        latency_warnings.append(
            "manifest.latency_profile is missing — add a latency profile reference "
            "to avoid Gate D rejection (see config/research/latency_profiles.yaml)"
        )
    else:
        latency_profile_present = True
        try:
            from hft_platform.alpha.latency_profiles import _ALIASES, load_profiles

            profiles = load_profiles()
            if profiles:
                profile_id = str(manifest_latency)
                resolved_id = _ALIASES.get(profile_id, profile_id)
                if resolved_id in profiles or profile_id in profiles:
                    latency_profile_valid = True
                else:
                    latency_warnings.append(
                        f"manifest.latency_profile '{profile_id}' not found in "
                        f"latency_profiles.yaml (available: {sorted(profiles.keys())})"
                    )
            else:
                latency_profile_valid = True
        except Exception as _exc:  # noqa: BLE001
            latency_profile_valid = True

    latency_profile_passed = (not enforce_latency) or latency_profile_present

    passed = (
        not missing_fields_by_path
        and complexity_ok
        and paper_governance_passed
        and data_governance_passed
        and latency_profile_passed
    )
    achieved_values = list(data_ul_achieved_by_path.values())
    data_ul_achieved_min = min(achieved_values) if achieved_values else None
    return GateReport(
        gate="Gate A",
        passed=passed,
        details={
            "missing_fields": missing_fields,
            "missing_fields_by_path": missing_fields_by_path,
            "available_fields": sorted(available_fields_union),
            "checked_data_paths": list(data_paths),
            "required_fields": required,
            "complexity": raw_complexity,
            "complexity_ok": complexity_ok,
            "precision_warnings": precision_warnings,
            "paper_refs": paper_refs,
            "paper_governance": {
                "require_paper_refs": require_paper_refs,
                "require_paper_index_link": require_paper_index_link,
                "paper_ref_missing": paper_ref_missing,
                "unresolved_paper_refs": unresolved_paper_refs,
                "unmapped_paper_refs": unmapped_paper_refs,
                "passed": paper_governance_passed,
            },
            "data_governance": {
                "enforced": enforce_data_governance,
                "require_data_meta": require_data_meta,
                "data_ul_target": data_ul_target,
                "data_ul_achieved": data_ul_achieved_min,
                "data_ul_achieved_by_path": data_ul_achieved_by_path,
                "data_ul_missing_fields": data_ul_missing_fields_by_path,
                "warnings": data_ul_warnings,
                "required_data_provenance_fields": list(required_data_provenance_fields),
                "allowed_data_roots": allowed_roots,
                "invalid_data_roots": invalid_data_roots,
                "invalid_data_formats": invalid_data_formats,
                "missing_data_metadata": missing_data_metadata,
                "invalid_data_metadata": invalid_data_metadata,
                "passed": data_governance_passed,
            },
            "skills_governance": {
                "roles_used": roles_used,
                "skills_used": skills_used,
                "invalid_roles": invalid_roles_list,
                "invalid_skills": invalid_skills_list,
                "warnings": skills_warnings,
            },
            "latency_profile": {
                "enforce": enforce_latency,
                "present": latency_profile_present,
                "valid": latency_profile_valid,
                "manifest_value": str(manifest_latency) if manifest_latency else None,
                "warnings": latency_warnings,
                "passed": latency_profile_passed,
            },
        },
    )


def _load_data_fields(path: str) -> set[str]:
    source = np.load(path, allow_pickle=False)
    try:
        if isinstance(source, np.lib.npyio.NpzFile):
            if "data" not in source:
                return set()
            arr = np.asarray(source["data"])
        else:
            arr = np.asarray(source)
    finally:
        if isinstance(source, np.lib.npyio.NpzFile):
            source.close()

    if arr.dtype.names:
        return set(str(name) for name in arr.dtype.names)
    return set()


def _check_hftbacktest_v2_data_format(path: str) -> list[str]:
    errors = []
    if not path.endswith(".npz"):
        errors.append("File is not a .npz archive, hftbacktest V2 requires AOS .npz format")

    try:
        source = np.load(path, allow_pickle=False)
        try:
            if isinstance(source, np.lib.npyio.NpzFile):
                if "data" not in source:
                    return errors + ["Missing 'data' array in .npz"]
                arr = np.asarray(source["data"])
            else:
                arr = np.asarray(source)
        finally:
            if hasattr(source, "close"):
                source.close()

        names = arr.dtype.names
        if not names:
            errors.append("Dataset is not a structured array (AOS format required)")
            return errors

        for field in ["exch_ts", "local_ts"]:
            if field in names:
                if arr.dtype[field].kind not in ("i", "u") or arr.dtype[field].itemsize != 8:
                    errors.append(f"'{field}' must be int64/uint64 nanoseconds")
            else:
                errors.append(f"Missing required field '{field}'")

        if "ev" in names:
            if arr.dtype["ev"].kind not in ("i", "u"):
                errors.append("'ev' flag must be an integer field")
            # Note: previous versions of this check additionally asserted that
            # ``first_ev & DEPTH_SNAPSHOT_EVENT`` was set. That assertion was
            # never satisfied by real CK exports (which start with regular
            # DEPTH+BUY/SELL events), so it produced spurious errors for every
            # valid corpus file -- bypassed only by alphas with empty
            # ``data_fields`` (e.g. c75). The check has been removed in favour
            # of ``research/backtest/_npz_format.py::detect_npz_format``,
            # which is consumed by the runner when ``feature_mode`` requires
            # L5 depth. See docs/runbooks/npz-formats-2026-05-06.md.
        else:
            errors.append("Missing required field 'ev'")

    except Exception as e:
        errors.append(f"Failed to load numpy array: {e}")

    return errors


def _field_available(field: str, available: set[str]) -> bool:
    if field == "current_mid":
        if ("best_bid" in available and "best_ask" in available) or ("bid_px" in available and "ask_px" in available):
            return True
    if field in available:
        return True
    aliases = _FIELD_ALIASES.get(field, ())
    return any(alias in available for alias in aliases)


def _load_paper_index(root: Path | None) -> dict[str, Any]:
    if root is None:
        return {}
    index_path = root / "research" / "knowledge" / "paper_index.json"
    if not index_path.exists():
        return {}
    try:
        payload = json.loads(index_path.read_text())
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_paper_ref(ref: str, paper_index: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
    if ref in paper_index and isinstance(paper_index[ref], dict):
        return ref, paper_index[ref]
    for key, row in paper_index.items():
        if not isinstance(row, dict):
            continue
        if str(row.get("arxiv_id", "")).strip() == ref:
            return str(key), row
    return None, None


def _load_dataset_metadata(data_path: Path) -> tuple[dict[str, Any] | None, Path | None, str | None]:
    for meta_path in _dataset_metadata_candidates(data_path):
        if not meta_path.exists():
            continue
        try:
            payload = json.loads(meta_path.read_text())
        except (OSError, ValueError) as exc:
            return None, meta_path, f"invalid_json:{exc}"
        if not isinstance(payload, dict):
            return None, meta_path, "invalid_format"
        return payload, meta_path, None
    return None, None, "missing_meta_file"


def _validate_dataset_metadata(meta: dict[str, Any], data_path: Path) -> list[str]:
    problems: list[str] = []
    required_keys = (
        "dataset_id",
        "source_type",
        "owner",
        "schema_version",
        "rows",
        "fields",
    )
    for key in required_keys:
        if key not in meta:
            problems.append(f"missing:{key}")

    source_type = str(meta.get("source_type", "")).strip().lower()
    if source_type and source_type not in {"synthetic", "real"}:
        problems.append("source_type_must_be_synthetic_or_real")

    try:
        schema_version = int(meta.get("schema_version", 0))
        if schema_version < 1:
            problems.append("schema_version_must_be>=1")
    except (TypeError, ValueError):
        problems.append("schema_version_not_int")

    try:
        rows_meta = int(meta.get("rows", -1))
        if rows_meta <= 0:
            problems.append("rows_must_be>0")
    except (TypeError, ValueError):
        rows_meta = -1
        problems.append("rows_not_int")

    actual_rows = _dataset_row_count(data_path)
    if actual_rows is not None and rows_meta > 0 and rows_meta != actual_rows:
        problems.append(f"rows_mismatch(meta={rows_meta},actual={actual_rows})")

    fields = meta.get("fields")
    if not isinstance(fields, list) or not fields:
        problems.append("fields_must_be_nonempty_list")
    return problems
