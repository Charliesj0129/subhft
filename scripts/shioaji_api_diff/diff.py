"""Structured diff between two captured Shioaji surface snapshots.

Produces a flat, deterministic list of ``ChangeRecord`` dicts. Classification
(``platform_used`` / breaking-vs-additive / remediation) is layered on by
``classify.py`` — this module only computes *what* changed, not *how bad*.
"""

from __future__ import annotations

from typing import Any

ChangeRecord = dict[str, Any]


def _present(node: Any) -> bool:
    return isinstance(node, dict) and node.get("_present", True) is True


def _rec(section: str, qualname: str, kind: str, before: Any, after: Any,
         **extra: Any) -> ChangeRecord:
    rec: ChangeRecord = {"section": section, "qualname": qualname, "kind": kind,
                         "before": before, "after": after}
    rec.update(extra)
    return rec


# --------------------------------------------------------------------------- #
def _diff_layout(a: dict[str, Any], b: dict[str, Any]) -> list[ChangeRecord]:
    out: list[ChangeRecord] = []
    if not (_present(a) and _present(b)):
        return out
    if a.get("is_compiled") != b.get("is_compiled"):
        out.append(_rec("layout", "package.is_compiled", "package_recompiled",
                        a.get("is_compiled"), b.get("is_compiled")))
    sa, sb = a.get("submodules", {}), b.get("submodules", {})
    for mod in sorted(set(sa) | set(sb)):
        if sa.get(mod) and not sb.get(mod):
            out.append(_rec("layout", mod, "submodule_removed", True, False))
        elif sb.get(mod) and not sa.get(mod):
            out.append(_rec("layout", mod, "submodule_added", False, True))
    return out


def _diff_enums(a: dict[str, Any], b: dict[str, Any]) -> list[ChangeRecord]:
    out: list[ChangeRecord] = []
    for name in sorted(set(a) | set(b)):
        ea, eb = a.get(name, {}), b.get(name, {})
        if _present(ea) and not _present(eb):
            out.append(_rec("enum", name, "enum_removed", "present", "absent", enum=name))
            continue
        if not _present(ea) and _present(eb):
            out.append(_rec("enum", name, "enum_added", "absent", "present", enum=name))
            continue
        if not (_present(ea) and _present(eb)):
            continue
        if ea.get("source") != eb.get("source"):
            out.append(_rec("enum", name, "enum_relocated", ea.get("source"),
                            eb.get("source"), enum=name))
        ma, mb = ea.get("members", {}), eb.get("members", {})
        removed = {k: ma[k] for k in ma if k not in mb}
        added = {k: mb[k] for k in mb if k not in ma}
        renamed: list[tuple[str, str, Any]] = []
        for rk, rv in list(removed.items()):
            match = [ak for ak, av in added.items() if av == rv and ak not in {x[1] for x in renamed}]
            if match:
                ak = sorted(match)[0]
                renamed.append((rk, ak, rv))
                removed.pop(rk, None)
                added.pop(ak, None)
        for rk, ak, rv in renamed:
            out.append(_rec("enum", f"{name}.{rk}", "enum_member_renamed", rk, ak, enum=name, value=rv))
        for k in sorted(removed):
            out.append(_rec("enum", f"{name}.{k}", "enum_member_removed", ma[k], None, enum=name))
        for k in sorted(added):
            out.append(_rec("enum", f"{name}.{k}", "enum_member_added", None, mb[k], enum=name))
        for k in sorted(set(ma) & set(mb)):
            if ma[k] != mb[k]:
                out.append(_rec("enum", f"{name}.{k}", "enum_value_changed", ma[k], mb[k], enum=name))
    return out


def _diff_models(a: dict[str, Any], b: dict[str, Any]) -> list[ChangeRecord]:
    out: list[ChangeRecord] = []
    for name in sorted(set(a) | set(b)):
        ma, mb = a.get(name, {}), b.get(name, {})
        if _present(ma) and not _present(mb):
            out.append(_rec("model", name, "model_removed", "present", "absent", model=name))
            continue
        if not _present(ma) and _present(mb):
            out.append(_rec("model", name, "model_added", "absent", "present", model=name))
            continue
        if not (_present(ma) and _present(mb)):
            continue
        if ma.get("kind") != mb.get("kind"):
            out.append(_rec("model", f"{name}.__kind__", "model_kind_changed",
                            ma.get("kind"), mb.get("kind"), model=name))
        # Compare the field-NAME surface as fields ∪ attributes, so a field that
        # moved from a pydantic/annotated field to a compiled-struct attribute
        # (1.5.x PyO3 rewrite) is NOT reported as a spurious removal.
        fa, fb = ma.get("fields", {}), mb.get("fields", {})
        names_a = set(fa) | set(ma.get("attributes", []))
        names_b = set(fb) | set(mb.get("attributes", []))
        for f in sorted(names_a - names_b):
            out.append(_rec("model", f"{name}.{f}", "field_removed", fa.get(f), None, model=name))
        for f in sorted(names_b - names_a):
            out.append(_rec("model", f"{name}.{f}", "field_added", None, fb.get(f), model=name))
        # Type/required drift only where both sides expose typed field metadata.
        for f in sorted(set(fa) & set(fb)):
            if fa[f].get("type") != fb[f].get("type"):
                out.append(_rec("model", f"{name}.{f}", "field_type_changed",
                                fa[f].get("type"), fb[f].get("type"), model=name))
            if fa[f].get("required") != fb[f].get("required"):
                out.append(_rec("model", f"{name}.{f}", "field_required_changed",
                                fa[f].get("required"), fb[f].get("required"), model=name))
    return out


def _params_by_name(rec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["name"]: p for p in (rec.get("params") or [])}


def _diff_methods(a: dict[str, Any], b: dict[str, Any]) -> list[ChangeRecord]:
    out: list[ChangeRecord] = []
    for cls in sorted(set(a) | set(b)):
        ca, cb = a.get(cls, {}), b.get(cls, {})
        if _present(ca) and not _present(cb):
            out.append(_rec("method", cls, "class_removed", "present", "absent", cls=cls, method=""))
            continue
        ma = ca.get("members", {}) if _present(ca) else {}
        mb = cb.get("members", {}) if _present(cb) else {}
        for meth in sorted(set(ma) | set(mb)):
            ra, rb = ma.get(meth, {}), mb.get(meth, {})
            qual = f"{cls}.{meth}"
            if _present(ra) and not _present(rb):
                out.append(_rec("method", qual, "method_removed", "present", "absent",
                                method=meth, cls=cls))
                continue
            if not _present(ra) and _present(rb):
                out.append(_rec("method", qual, "method_added", "absent", "present",
                                method=meth, cls=cls))
                continue
            if not (_present(ra) and _present(rb)):
                continue
            pa, pb = _params_by_name(ra), _params_by_name(rb)
            for p in sorted(set(pa) - set(pb)):
                out.append(_rec("method", f"{qual}({p})", "param_removed", pa[p], None,
                                method=meth, cls=cls, param=p))
            for p in sorted(set(pb) - set(pa)):
                required = pb[p].get("default") == "<empty>" and pb[p]["kind"] in {
                    "POSITIONAL_OR_KEYWORD", "POSITIONAL_ONLY", "KEYWORD_ONLY"}
                out.append(_rec("method", f"{qual}({p})", "param_added", None, pb[p],
                                method=meth, cls=cls, param=p, required=required))
            for p in sorted(set(pa) & set(pb)):
                if pa[p].get("default") != "<empty>" and pb[p].get("default") == "<empty>":
                    out.append(_rec("method", f"{qual}({p})", "param_default_removed",
                                    pa[p].get("default"), "<empty>", method=meth, cls=cls, param=p))
                elif pa[p].get("default") != pb[p].get("default"):
                    out.append(_rec("method", f"{qual}({p})", "param_default_changed",
                                    pa[p].get("default"), pb[p].get("default"),
                                    method=meth, cls=cls, param=p))
    return out


def _diff_config(a: dict[str, Any], b: dict[str, Any]) -> list[ChangeRecord]:
    out: list[ChangeRecord] = []
    if _present(a) and not _present(b):
        out.append(_rec("config", a.get("module") or "shioaji.config",
                        "config_module_removed", "present", "absent"))
    da = a.get("defaults", {}) if isinstance(a, dict) else {}
    db = b.get("defaults", {}) if isinstance(b, dict) else {}
    for key in sorted(set(da) | set(db)):
        va, vb = da.get(key, {}), db.get(key, {})
        pa, pb = _present(va), _present(vb)
        if pa and not pb:
            out.append(_rec("config", key, "config_removed", va.get("value"), None))
        elif pb and not pa:
            out.append(_rec("config", key, "config_added", None, vb.get("value")))
        elif pa and pb and va.get("value") != vb.get("value"):
            out.append(_rec("config", key, "config_value_changed", va.get("value"), vb.get("value")))
    return out


def _diff_exceptions(a: dict[str, Any], b: dict[str, Any]) -> list[ChangeRecord]:
    out: list[ChangeRecord] = []
    for name in sorted(set(a) | set(b)):
        ea, eb = a.get(name, {}), b.get(name, {})
        if _present(ea) and not _present(eb):
            out.append(_rec("exception", name, "exception_removed", "present", "absent"))
        elif _present(eb) and not _present(ea):
            out.append(_rec("exception", name, "exception_added", "absent", "present"))
        elif _present(ea) and _present(eb) and ea.get("bases") != eb.get("bases"):
            out.append(_rec("exception", name, "exception_bases_changed",
                            ea.get("bases"), eb.get("bases")))
    return out


def _diff_compiled(a: dict[str, Any], b: dict[str, Any]) -> list[ChangeRecord]:
    out: list[ChangeRecord] = []
    for mod in sorted(set(a) | set(b)):
        ma, mb = a.get(mod, {}), b.get(mod, {})
        if _present(ma) and not _present(mb):
            out.append(_rec("compiled", mod, "compiled_module_removed", "present", "absent",
                            symbol=mod))
            continue
        if _present(mb) and not _present(ma):
            out.append(_rec("compiled", mod, "compiled_module_added", "absent", "present"))
            continue
        if not (_present(ma) and _present(mb)):
            continue
        for cls_name in sorted(set(ma.get("classes", {})) | set(mb.get("classes", {}))):
            cma = ma.get("classes", {}).get(cls_name, {})
            cmb = mb.get("classes", {}).get(cls_name, {})
            meths_a = cma.get("methods", {}) if _present(cma) else {}
            meths_b = cmb.get("methods", {}) if _present(cmb) else {}
            for meth in sorted(set(meths_a) | set(meths_b)):
                wa, wb = meths_a.get(meth, {}), meths_b.get(meth, {})
                qual = f"{mod}.{cls_name}.{meth}"
                if _present(wa) and not _present(wb):
                    out.append(_rec("compiled", qual, "sol_wrap_removed", "present", "absent",
                                    symbol=meth))
                elif _present(wb) and not _present(wa):
                    out.append(_rec("compiled", qual, "sol_wrap_added", "absent", "present",
                                    symbol=meth))
                elif _present(wa) and _present(wb) and wa.get("param_count") != wb.get("param_count"):
                    out.append(_rec("compiled", qual, "sol_wrap_arity_changed",
                                    wa.get("param_count"), wb.get("param_count"), symbol=meth))
        aa, ab = set(ma.get("module_attrs", [])), set(mb.get("module_attrs", []))
        if aa != ab:
            out.append(_rec("compiled", f"{mod}.module_attrs", "compiled_attrs_changed",
                            sorted(aa - ab), sorted(ab - aa)))
    return out


def diff_snapshots(old: dict[str, Any], new: dict[str, Any]) -> list[ChangeRecord]:
    """Return the full flat list of raw (unclassified) change records."""
    records: list[ChangeRecord] = []
    records += _diff_layout(old.get("package_layout", {}), new.get("package_layout", {}))
    records += _diff_enums(old.get("constants", {}), new.get("constants", {}))
    records += _diff_models(old.get("models", {}), new.get("models", {}))
    records += _diff_methods(old.get("methods", {}), new.get("methods", {}))
    records += _diff_config(old.get("config", {}), new.get("config", {}))
    records += _diff_exceptions(old.get("exceptions", {}), new.get("exceptions", {}))
    records += _diff_compiled(old.get("compiled", {}), new.get("compiled", {}))
    return records
