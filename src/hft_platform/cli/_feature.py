"""CLI commands: feature profiles, rollout, validate, preflight."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def cmd_feature_profiles(args: argparse.Namespace) -> None:
    from hft_platform.feature.profile import load_feature_profile_registry

    reg = load_feature_profile_registry(getattr(args, "path", None))
    payload = reg.to_dict()
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(f"Feature profiles path: {payload.get('path')}")
    print(f"Default profile: {payload.get('default_profile_id')}")
    for prof in payload.get("profiles", []):
        print(
            f"- {prof['profile_id']} set={prof['feature_set_id']} state={prof.get('state')} "
            f"enabled={prof.get('enabled')} params={prof.get('params')}"
        )
    errs = payload.get("errors") or []
    if errs:
        print("Validation errors:")
        for e in errs:
            print(f"  - {e}")


def cmd_feature_rollout_status(args: argparse.Namespace) -> None:
    from hft_platform.feature.profile import load_feature_profile_registry
    from hft_platform.feature.rollout import load_feature_rollout_controller

    reg = load_feature_profile_registry(getattr(args, "profiles", None))
    ctrl = load_feature_rollout_controller(getattr(args, "state_path", None))
    fsid = str(getattr(args, "feature_set", "") or "").strip() or None
    payload: dict[str, Any] = {
        "feature_profiles_path": reg.path,
        "rollout_state_path": ctrl.path,
        "rollout_version": ctrl.version,
        "sets": [],
    }
    for assignment in ctrl.assignments():
        if fsid and assignment.feature_set_id != fsid:
            continue
        resolved = ctrl.resolve_profile_id(assignment.feature_set_id)
        prof_exists = bool(resolved and resolved in set(reg.ids()))
        payload["sets"].append(
            {
                **assignment.to_dict(),
                "resolved_profile_id": resolved,
                "resolved_profile_exists": prof_exists,
            }
        )
    if fsid and not payload["sets"]:
        payload["sets"].append(
            {
                "feature_set_id": fsid,
                "state": None,
                "resolved_profile_id": None,
                "resolved_profile_exists": False,
                "note": "no rollout assignment yet",
            }
        )
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def cmd_feature_rollout_set(args: argparse.Namespace) -> None:
    from hft_platform.feature.profile import load_feature_profile_registry
    from hft_platform.feature.rollout import load_feature_rollout_controller

    reg = load_feature_profile_registry(getattr(args, "profiles", None))
    ctrl = load_feature_rollout_controller(getattr(args, "state_path", None))
    fsid = str(args.feature_set)
    state = str(args.state)
    profile_id = getattr(args, "profile_id", None)
    if profile_id:
        profile_id = str(profile_id)
        try:
            prof = reg.get(profile_id)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"unknown profile_id {profile_id!r}: {exc}"}, indent=2))
            sys.exit(1)
        if prof.feature_set_id != fsid:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": f"profile {profile_id!r} targets feature_set {prof.feature_set_id!r}, not {fsid!r}",
                    },
                    indent=2,
                )
            )
            sys.exit(1)
    try:
        assignment = ctrl.set_assignment(
            feature_set_id=fsid,
            state=state,
            profile_id=profile_id,
            actor=str(getattr(args, "actor", "cli") or "cli"),
            notes=str(getattr(args, "notes", "") or ""),
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "path": ctrl.path}, indent=2, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps({"ok": True, "path": ctrl.path, "assignment": assignment.to_dict()}, indent=2, ensure_ascii=False))


def cmd_feature_rollout_rollback(args: argparse.Namespace) -> None:
    from hft_platform.feature.rollout import load_feature_rollout_controller

    ctrl = load_feature_rollout_controller(getattr(args, "state_path", None))
    try:
        assignment = ctrl.rollback(
            feature_set_id=str(args.feature_set),
            actor=str(getattr(args, "actor", "cli") or "cli"),
            notes=str(getattr(args, "notes", "") or "rollback"),
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc), "path": ctrl.path}, indent=2, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps({"ok": True, "path": ctrl.path, "assignment": assignment.to_dict()}, indent=2, ensure_ascii=False))


def cmd_feature_validate(args: argparse.Namespace) -> None:
    from hft_platform.feature.compat import check_feature_profile_compat, check_runtime_feature_engine_compat
    from hft_platform.feature.engine import FeatureEngine
    from hft_platform.feature.profile import load_feature_profile_registry

    reg = load_feature_profile_registry(getattr(args, "path", None))
    errors = list(reg.validate())
    fe = FeatureEngine()
    errors.extend(i.message for i in check_runtime_feature_engine_compat(fe) if i.level == "error")
    profile = reg.get_active_for_set(fe.feature_set_id()) if reg.ids() else None
    applied = None
    if profile is not None:
        prof_issues = check_feature_profile_compat(profile, fe._registry)  # prototype: introspect engine registry
        errors.extend(i.message for i in prof_issues if i.level == "error")
        try:
            fe.apply_profile(profile)
            applied = profile.profile_id
        except Exception as exc:
            errors.append(f"Failed to apply active profile {profile.profile_id!r}: {exc}")
    out = {
        "ok": not errors,
        "feature_set_id": fe.feature_set_id(),
        "schema_version": fe.schema_version(),
        "active_profile_id": applied,
        "errors": errors,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    if errors:
        sys.exit(1)


def cmd_feature_preflight(args: argparse.Namespace) -> None:
    from hft_platform.feature.engine import FeatureEngine
    from hft_platform.feature.profile import load_feature_profile_registry
    from hft_platform.strategy.compat import check_strategies_feature_compat
    from hft_platform.strategy.registry import StrategyRegistry

    reg = load_feature_profile_registry(getattr(args, "profiles", None))
    fe = FeatureEngine()
    active_profile = reg.get_active_for_set(fe.feature_set_id()) if reg.ids() else None
    if active_profile is not None:
        fe.apply_profile(active_profile)
    sreg = StrategyRegistry(getattr(args, "strategies", "config/base/strategies.yaml"))
    strategies = sreg.instantiate()
    issues = [i.to_dict() for i in check_strategies_feature_compat(strategies, fe)]
    out = {
        "feature_set_id": fe.feature_set_id(),
        "feature_schema_version": fe.schema_version(),
        "feature_profile_id": fe.active_profile_id(),
        "strategy_count": len(strategies),
        "issues": issues,
        "ok": not any(i["level"] == "error" for i in issues),
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    if not out["ok"]:
        sys.exit(1)
