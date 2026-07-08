"""Classify raw diff records from the platform's perspective.

Adds ``platform_used``, ``classification``, ``shimmable`` and ``remediation`` to
each record, then rolls the set up into a single verdict that drives the
upgrade go/no-go decision.

Classifications:
  BREAKING       a change the platform depends on that will misbehave/raise.
  ADDITIVE       a new capability; no action required.
  BEHAVIORAL     runtime behavior may shift (SOL_* defaults, shim arity); review.
  BENIGN         changed in the SDK but the platform never touches it.
  INFORMATIONAL  coarse opaque-layer signal; impact not statically assessable.

Verdicts:
  SAFE        no platform-impacting BREAKING change.
  NEEDS-SHIM  BREAKING changes exist but all are mechanically shimmable
              (e.g. enum-member rename -> add an alias in order_codec.py).
  BLOCKED     BREAKING changes requiring real adapter work before upgrade.
"""

from __future__ import annotations

from typing import Any

from . import platform_contract as pc

BREAKING = "BREAKING"
ADDITIVE = "ADDITIVE"
BEHAVIORAL = "BEHAVIORAL"
BENIGN = "BENIGN"
INFORMATIONAL = "INFORMATIONAL"

# Order used for stable sorting (most severe first) in reports.
_RANK = {BREAKING: 0, BEHAVIORAL: 1, ADDITIVE: 2, INFORMATIONAL: 3, BENIGN: 4}

# Kinds whose BREAKING form can be fixed by a mechanical adapter shim/alias
# rather than a structural change.
_SHIMMABLE_KINDS = {"enum_member_renamed"}


def _classify_one(rec: dict[str, Any]) -> tuple[str, bool, bool, str]:
    """Return (classification, platform_used, shimmable, remediation_key)."""
    kind = rec["kind"]

    # ---- package layout (architecture) ----------------------------------- #
    if rec["section"] == "layout":
        if kind == "package_recompiled":
            # The headline: pure-Python -> compiled rewrite. Not itself a code
            # break, but it invalidates assumptions across the adapter.
            return INFORMATIONAL, True, False, "layout"
        if kind == "submodule_removed":
            mod = rec["qualname"]
            if mod.startswith("shioaji.backend"):
                return BREAKING, True, False, "sol_wrap"      # arity-shim target gone
            if mod == "shioaji.config":
                return BEHAVIORAL, True, False, "sol_config"  # SOL_* knobs gone
            if mod in {"shioaji.shioaji", "shioaji.main"}:
                return BEHAVIORAL, True, False, "layout"      # import path moved
            return INFORMATIONAL, False, False, "layout"
        if kind == "submodule_added":
            return ADDITIVE, False, False, ""
        return INFORMATIONAL, False, False, "layout"

    # ---- enums ----------------------------------------------------------- #
    if rec["section"] == "enum":
        enum = rec.get("enum", rec["qualname"])
        if kind in {"enum_member_added", "enum_added"}:
            return ADDITIVE, False, False, ""
        if kind == "enum_relocated":
            used = pc.enum_class_used(enum)
            return (BEHAVIORAL if used else BENIGN), used, False, f"enum:{enum}"
        if kind in {"enum_member_removed", "enum_member_renamed", "enum_value_changed"}:
            member = rec["qualname"].split(".", 1)[1] if "." in rec["qualname"] else ""
            used = pc.enum_member_used(enum, member)
            cls = (BREAKING if used else BENIGN) if kind != "enum_value_changed" else (
                BEHAVIORAL if used else BENIGN)
            return cls, used, kind in _SHIMMABLE_KINDS, f"enum:{enum}"
        if kind == "enum_removed":
            used = pc.enum_class_used(enum)
            return (BREAKING if used else BENIGN), used, False, f"enum:{enum}"

    # ---- models ---------------------------------------------------------- #
    if rec["section"] == "model":
        model = rec.get("model", rec["qualname"])
        used_model = model in pc.CTOR_FIELDS or model in pc.READ_MODEL_QUALS
        if kind in {"field_added", "model_added"}:
            return ADDITIVE, False, False, ""
        if kind == "model_kind_changed":
            # e.g. pydantic -> compiled struct: attribute reads may still work,
            # but field validation/construction semantics shift. Flag if used.
            return (BEHAVIORAL if used_model else INFORMATIONAL), used_model, False, f"ctor:{model}"
        if kind == "model_removed":
            return (BREAKING if used_model else BENIGN), used_model, False, f"ctor:{model}"
        if kind in {"field_removed", "field_type_changed", "field_required_changed"}:
            field = rec["qualname"].rsplit(".", 1)[1]
            if kind == "field_required_changed" and rec.get("after") is False:
                return ADDITIVE, False, False, ""  # became optional — safe
            used = pc.model_field_used(model, field)
            return (BREAKING if used else BENIGN), used, False, f"ctor:{model}"

    # ---- methods --------------------------------------------------------- #
    if rec["section"] == "method":
        cls, meth = rec.get("cls", ""), rec.get("method", "")
        if kind == "method_added":
            return ADDITIVE, False, False, ""
        if kind == "class_removed":
            # A removed class is a hard break ONLY if the platform constructs it
            # by name (the Shioaji instance, or the order ctors). Classes the
            # adapter reaches via an instance accessor keep working when the
            # module-level symbol is folded away. VERIFIED on installed 1.5.3
            # (see runbook "Deep reverse-engineering verification"): the Quote
            # class is gone from the namespace, but `api.quote` survives as a
            # property returning `_QuoteProxy`, which still exposes
            # subscribe/unsubscribe, the v1 tick/bidask callbacks and
            # set_event_callback — so the market-data + reconnect paths are
            # intact. Hence constructed-by-name -> BREAKING; accessor-reached ->
            # BEHAVIORAL (review the instance path; do not assume an outage).
            constructed_by_name = cls == "Shioaji" or cls in pc.CTOR_FIELDS
            used = constructed_by_name or cls in pc.EXISTENCE_CRITICAL_METHODS
            if constructed_by_name:
                return BREAKING, True, False, "class_removed"
            return BEHAVIORAL, used, False, "class_removed"
        if kind == "method_removed":
            used = pc.method_existence_critical(cls, meth)
            return (BREAKING if used else BENIGN), used, False, f"method:{meth}"
        if kind == "param_added":
            if not rec.get("required"):
                return ADDITIVE, False, False, ""
            # A new REQUIRED param breaks every caller of a method we call.
            used = pc.method_existence_critical(cls, meth) or meth in pc.METHOD_PARAMS
            return (BREAKING if used else BENIGN), used, False, f"method:{meth}"
        if kind == "param_removed":
            used = pc.method_param_used(meth, rec.get("param", ""))
            return (BREAKING if used else BENIGN), used, False, f"method:{meth}"
        if kind == "param_default_removed":
            used = pc.method_existence_critical(cls, meth) or meth in pc.METHOD_PARAMS
            return (BREAKING if used else BENIGN), used, False, f"method:{meth}"
        if kind == "param_default_changed":
            used = pc.method_existence_critical(cls, meth) or meth in pc.METHOD_PARAMS
            return (BEHAVIORAL if used else BENIGN), used, False, f"method:{meth}"
        if kind == "return_changed":
            return INFORMATIONAL, False, False, ""

    # ---- config (SOL_*) -------------------------------------------------- #
    if rec["section"] == "config":
        used = rec["qualname"] in pc.SOL_CONFIG_KEYS
        return BEHAVIORAL, used, False, "sol_config"

    # ---- exceptions ------------------------------------------------------ #
    if rec["section"] == "exception":
        if kind == "exception_added":
            return ADDITIVE, False, False, ""
        name = rec["qualname"]
        used = name in pc.EXCEPTIONS_CAUGHT
        if kind == "exception_removed":
            return (BREAKING if used else BENIGN), used, False, ""
        return (BEHAVIORAL if used else BENIGN), used, False, ""

    # ---- compiled (.so) -------------------------------------------------- #
    if rec["section"] == "compiled":
        if kind == "sol_wrap_removed":
            arity = pc.sol_wrap_expected_arity(rec.get("symbol", ""))
            return BREAKING, arity is not None, False, "sol_wrap"
        if kind == "sol_wrap_arity_changed":
            # Shim forwards a fixed prefix; an arity change means it may now be
            # unnecessary OR mis-sized — flag for explicit re-validation.
            arity = pc.sol_wrap_expected_arity(rec.get("symbol", ""))
            return BEHAVIORAL, arity is not None, False, "sol_wrap"
        if kind == "sol_wrap_added":
            return ADDITIVE, False, False, ""
        if kind in {"compiled_module_removed"}:
            return BREAKING, True, False, "sol_wrap"
        return INFORMATIONAL, False, False, ""

    return INFORMATIONAL, False, False, ""


def classify_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in records:
        classification, used, shimmable, rem_key = _classify_one(rec)
        annotated = dict(rec)
        annotated["classification"] = classification
        annotated["platform_used"] = used
        annotated["shimmable"] = shimmable
        annotated["remediation"] = pc.remediation(rem_key) if rem_key else ""
        out.append(annotated)
    out.sort(key=lambda r: (_RANK.get(r["classification"], 9), not r["platform_used"],
                            r["section"], r["qualname"]))
    return out


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    breaking_used = [r for r in records if r["classification"] == BREAKING and r["platform_used"]]
    counts = {
        "breaking_platform": len(breaking_used),
        "breaking_other": sum(1 for r in records
                              if r["classification"] == BREAKING and not r["platform_used"]),
        "additive": sum(1 for r in records if r["classification"] == ADDITIVE),
        "behavioral": sum(1 for r in records if r["classification"] == BEHAVIORAL),
        "benign": sum(1 for r in records if r["classification"] == BENIGN),
        "informational": sum(1 for r in records if r["classification"] == INFORMATIONAL),
    }
    if not breaking_used:
        verdict = "SAFE"
    elif all(r["shimmable"] for r in breaking_used):
        verdict = "NEEDS-SHIM"
    else:
        verdict = "BLOCKED"
    return {"verdict": verdict, "counts": counts}
