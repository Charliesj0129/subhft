"""In-venv Shioaji API-surface capture engine.

Run as::

    python -m scripts.shioaji_api_diff._capture_entrypoint --emit-json

This module imports ONLY the standard library plus ``shioaji`` so it can run
inside a bare throwaway venv that has nothing but the SDK installed. It performs
pure introspection — it NEVER calls ``api.login``, never opens a socket, never
reads ``.env``. Output is a deterministic JSON snapshot of the SDK surface on
stdout, byte-identical across runs of the same installed version.

The engine is *layout-agnostic*: each logical symbol is resolved from a priority
of locations (canonical top-level ``shioaji.*`` first, then the legacy
pure-Python submodules). This matters because shioaji ≤1.3.x is a pure-Python +
pydantic SDK exposing enums under ``shioaji.constant`` etc., while shioaji ≥1.5.x
is a compiled Rust extension (``shioaji._core``) that re-exports everything at
top level and leaves the old submodule paths as deprecation shims. Capturing by
logical name + recording the resolved source module turns an architecture
*relocation* into an honest diff signal instead of a false "everything removed".

The single public entry point ``build_surface_snapshot()`` is imported both by
the orchestrator (run inside each version's venv) and by the CI regression guard
(run in the repo venv against the already-installed SDK), so the golden file and
the live check can never diverge.
"""

from __future__ import annotations

import argparse
import datetime
import enum
import hashlib
import importlib
import importlib.metadata as ilm
import inspect
import json
import re
import shutil
import subprocess
import sys
import typing
import warnings
from typing import Any

# Bump when the snapshot JSON shape changes (forces golden regeneration).
SCHEMA_VERSION = 2
TOOL_VERSION = "1.0.0"

_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")
# Some SDK method defaults are computed at import (e.g. ``start=date.today()``),
# which would make the snapshot change every day. Neutralize them.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T].*)?$")
_MISSING = object()
_EMPTY = inspect.Parameter.empty

# --------------------------------------------------------------------------- #
# Declared logical targets. Each is resolved against a priority of modules
# (top-level canonical first, then legacy submodule). Declared lists are unioned
# with discovery so a REMOVED symbol still appears as ``{"_present": false}``
# (a removal is a diff signal, not a silent absence) and ADDED symbols are
# still captured.
# --------------------------------------------------------------------------- #
# logical enum name -> module search order
ENUM_SEARCH = ["shioaji", "shioaji.constant", "shioaji.account", "shioaji.contracts"]
DECLARED_ENUMS: list[str] = [
    "AccountType", "Action", "ChangeType", "Currency", "DayTrade", "Exchange",
    "FetchStatus", "FuturesOCType", "FuturesPriceType", "OptionRight",
    "OrderState", "OrderType", "QuoteType", "QuoteVersion", "ScannerType",
    "SecurityType", "Status", "StockOrderCond", "StockOrderLot",
    "StockPriceType", "TickType", "TicksQueryType", "TradeType", "Unit",
]

# logical model name -> module search order (top-level first)
MODEL_SEARCH = ["shioaji", "shioaji.order", "shioaji.account", "shioaji.position",
                "shioaji.stream_data_type", "shioaji.contracts", "shioaji.data"]
DECLARED_MODELS: list[str] = [
    "Account", "AccountBalance", "BaseOrder", "BidAskFOPv1", "BidAskSTKv1",
    "ComboContract", "ComboOrder", "Contract", "Contracts", "CreditEnquire",
    "DailyQuotes", "Deal", "Future", "FutureAccount", "FuturePosition",
    "FuturesOrder", "Index", "Kbars", "Margin", "Option", "Order",
    "OrderStatus", "Position", "PositionDetail", "ProfitLoss", "QuoteFOPv1",
    "QuoteSTKv1", "ScannerItem", "Settlement", "Snapshot", "Stock",
    "StockAccount", "StockOrder", "StockPosition", "TickFOPv1", "TickSTKv1",
    "Ticks", "Trade", "TradingLimits", "UsageStatus",
]

# logical client/quote class -> module search order
CLIENT_SEARCH = {
    "Shioaji": ["shioaji", "shioaji.shioaji"],
    "Quote": ["shioaji", "shioaji.shioaji"],
}
DECLARED_METHODS: dict[str, list[str]] = {
    "Shioaji": [
        "account_balance", "activate_ca", "cancel_order", "credit_enquires",
        "daily_quotes", "fetch_contracts", "kbars", "list_accounts",
        "list_position_detail", "list_positions", "list_profit_loss",
        "list_settlements", "list_trades", "login", "logout", "margin",
        "place_order", "scanners", "set_order_callback", "short_stock_sources",
        "snapshots", "subscribe", "subscribe_trade", "ticks", "trading_limits",
        "unsubscribe", "unsubscribe_trade", "update_order", "update_price",
        "update_qty", "update_status", "usage",
    ],
    "Quote": [
        "subscribe", "unsubscribe", "set_on_tick_stk_v1_callback",
        "set_on_tick_fop_v1_callback", "set_on_bidask_stk_v1_callback",
        "set_on_bidask_fop_v1_callback",
    ],
}

CONFIG_SEARCH = ["shioaji.config", "shioaji"]
DECLARED_SOL_CONFIG: list[str] = [
    "SOL_CONNECT_TIMEOUT_MS", "SOL_KEEP_ALIVE_LIMIT", "SOL_KEEP_ALIVE_MS",
    "SOL_RECONNECT_RETRIES", "SOL_RECONNECT_RETRY_WAIT",
]

DECLARED_EXCEPTIONS: list[str] = [
    "AccountError", "AccountNotProvideError", "AccountNotSignError", "BaseError",
    "CaError", "ContractError", "SystemMaintenance", "TargetContractNotExistError",
    "TimeoutError", "TokenError",
]
EXCEPTION_SEARCH = ["shioaji.error", "shioaji"]

# Compiled (.so) modules and the classes/methods worth reflecting. The six
# SolClient ``*_callback_wrap`` symbols are the arity-shim contract (their
# removal/rename silently no-ops the shim -> SIGABRT crash-loop returns).
DECLARED_COMPILED: dict[str, dict[str, list[str]]] = {
    "shioaji.backend.solace.api": {
        "SolClient": [
            "event_callback_wrap", "msg_callback_wrap", "onreply_callback_wrap",
            "p2p_callback_wrap", "reply_callback_wrap", "session_down_callback_wrap",
            "subscribe", "unsubscribe",
        ],
    },
    "shioaji.backend.solace.tick": {},
    "shioaji.backend.solace.bidask": {},
    "shioaji.backend.solace.quote": {},
    "shioaji.backend.solace.utils": {},
    "shioaji.backend.constant": {},
    "shioaji.backend.error": {},
    "shioaji.backend.utils": {},
    "shioaji._core": {},
}

# Legacy submodules whose presence/absence marks the pure-Python → compiled shift.
LAYOUT_SUBMODULES = [
    "shioaji.config", "shioaji.shioaji", "shioaji.main", "shioaji.backend",
    "shioaji.backend.solace.api", "shioaji.constant", "shioaji.order",
    "shioaji._core",
]


# --------------------------------------------------------------------------- #
# Determinism helpers — stable, version-independent, address-free reprs.
# --------------------------------------------------------------------------- #
def _scrub(text: str) -> str:
    return _HEX_RE.sub("<addr>", text).replace("typing.", "")


def _short(name: str) -> str:
    return name.rsplit(".", 1)[-1] if "." in name else name


def _constraints(metadata: Any) -> list[str]:
    out: list[str] = []
    items = metadata if isinstance(metadata, (list, tuple)) else [metadata]
    for m in items:
        if m is None:
            continue
        for attr, label in (
            ("gt", "gt"), ("ge", "ge"), ("lt", "lt"), ("le", "le"),
            ("max_length", "max_length"), ("min_length", "min_length"),
            ("max_digits", "max_digits"), ("decimal_places", "decimal_places"),
            ("multiple_of", "multiple_of"),
        ):
            val = getattr(m, attr, None)
            if val is not None and not callable(val):
                out.append(f"{label}={val}")
        if getattr(m, "strict", None) is True:
            out.append("strict")
    return sorted(set(out))


def normalize_type(tp: Any) -> str:
    if tp is None or tp is type(None):
        return "None"
    if tp is _EMPTY:
        return "Any"
    if isinstance(tp, list):  # Callable[[X, Y], R] yields [X, Y]
        return "[" + ", ".join(normalize_type(a) for a in tp) + "]"

    meta = getattr(tp, "__metadata__", None)
    if meta is not None:
        base = normalize_type(getattr(tp, "__origin__", Any))
        cons = _constraints(list(meta))
        return f"{base}[{','.join(cons)}]" if cons else base

    origin = typing.get_origin(tp)
    args = typing.get_args(tp)

    if origin is typing.Union or _short(type(tp).__name__) == "UnionType":
        non_none = [a for a in args if a is not type(None)]
        norm = sorted(normalize_type(a) for a in non_none)
        had_none = len(non_none) != len(args)
        body = norm[0] if len(norm) == 1 else f"Union[{', '.join(norm)}]"
        return f"Optional[{body}]" if had_none else body

    if origin is not None:
        builtins_map = {list: "list", dict: "dict", set: "set",
                        tuple: "tuple", frozenset: "frozenset"}
        oname = builtins_map.get(origin) or _short(getattr(origin, "__name__", str(origin)))
        if args:
            return f"{oname}[{', '.join(normalize_type(a) for a in args)}]"
        return _scrub(oname)

    name = getattr(tp, "__name__", None)
    if name:
        return _short(name)
    return _scrub(str(tp))


def normalize_default(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return f"{type(value).__name__}.{value.name}"
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return "<date>"
    if isinstance(value, str) and _DATE_RE.match(value):
        return "<date>"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return _scrub(repr(value))


def _is_pydantic_undefined(value: Any) -> bool:
    return type(value).__name__ in {"PydanticUndefinedType"} or repr(value) == "PydanticUndefined"


# --------------------------------------------------------------------------- #
# Resolution: find a logical symbol across a module search order.
# --------------------------------------------------------------------------- #
def _resolve(name: str, search: list[str]) -> tuple[Any, str | None]:
    for mod_name in search:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:  # noqa: BLE001 - module may not exist in this version
            continue
        obj = getattr(mod, name, _MISSING)
        if obj is not _MISSING:
            return obj, mod_name
    return _MISSING, None


# --------------------------------------------------------------------------- #
# Enum capture — handles Python ``enum.Enum`` and compiled (PyO3) enums.
# --------------------------------------------------------------------------- #
def _enum_members(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, type) and issubclass(obj, enum.Enum):
        return {m.name: normalize_default(m.value) for m in obj}
    if isinstance(obj, type):
        # PyO3 / compiled enum: variants are class attributes that are instances
        # of the enum type itself (e.g. ``Action.Buy`` is an ``Action``).
        members: dict[str, Any] = {}
        for attr in dir(obj):
            if attr.startswith("_"):
                continue
            try:
                val = getattr(obj, attr)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(val, obj):
                raw = getattr(val, "value", attr)
                members[attr] = normalize_default(raw) if not isinstance(raw, type) else attr
        if members:
            return dict(sorted(members.items()))
    return None


def capture_constants() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in sorted(set(DECLARED_ENUMS)):
        obj, source = _resolve(name, ENUM_SEARCH)
        members = _enum_members(obj) if obj is not _MISSING else None
        if members is None:
            result[name] = {"_present": False}
        else:
            result[name] = {"_present": True, "members": members, "source": source}
    return result


# --------------------------------------------------------------------------- #
# Field introspection — pydantic v2 / v1 / annotation-only / compiled.
# --------------------------------------------------------------------------- #
def _fields_pydantic_v2(cls: type) -> dict[str, dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}
    for name, fi in cls.model_fields.items():  # type: ignore[attr-defined]
        required = bool(fi.is_required()) if hasattr(fi, "is_required") else False
        has_default, default = False, None
        if getattr(fi, "default_factory", None) is not None:
            has_default, default = True, "<factory>"
        else:
            raw = getattr(fi, "default", _MISSING)
            if raw is not _MISSING and not _is_pydantic_undefined(raw):
                has_default, default = True, normalize_default(raw)
        type_repr = normalize_type(getattr(fi, "annotation", None))
        cons = _constraints(getattr(fi, "metadata", []) or [])
        if cons and "[" not in type_repr:
            type_repr = f"{type_repr}[{','.join(cons)}]"
        fields[name] = {"type": type_repr, "required": required,
                        "default": default, "has_default": has_default}
    return fields


def _fields_pydantic_v1(cls: type) -> dict[str, dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}
    for name, mf in cls.__fields__.items():  # type: ignore[attr-defined]
        type_obj = getattr(mf, "outer_type_", None) or getattr(mf, "type_", None)
        required = bool(getattr(mf, "required", False))
        has_default, default = False, None
        if getattr(mf, "default_factory", None) is not None:
            has_default, default = True, "<factory>"
        elif getattr(mf, "default", None) is not None:
            has_default, default = True, normalize_default(mf.default)
        fields[name] = {"type": normalize_type(type_obj), "required": required,
                        "default": default, "has_default": has_default}
    return fields


def _fields_annotated(cls: type) -> dict[str, dict[str, Any]]:
    try:
        hints = typing.get_type_hints(cls, include_extras=True)
    except Exception:
        hints = dict(getattr(cls, "__annotations__", {}))
    fields: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for klass in reversed(getattr(cls, "__mro__", [cls])):
        for name in getattr(klass, "__annotations__", {}):
            if name.startswith("_") or name in seen:
                continue
            seen.add(name)
            type_obj = hints.get(name, getattr(klass, "__annotations__", {}).get(name))
            has_default = hasattr(cls, name) and not isinstance(getattr(cls, name), type)
            default = normalize_default(getattr(cls, name)) if has_default else None
            fields[name] = {"type": normalize_type(type_obj), "required": not has_default,
                            "default": default, "has_default": has_default}
    return fields


def introspect_model(cls: type) -> dict[str, Any]:
    bases = sorted(_scrub(f"{b.__module__}.{getattr(b, '__qualname__', b.__name__)}")
                   for b in getattr(cls, "__bases__", ()))
    if isinstance(getattr(cls, "model_fields", None), dict):
        return {"kind": "pydantic2", "bases": bases, "fields": _fields_pydantic_v2(cls)}
    if getattr(cls, "__fields__", None):
        return {"kind": "pydantic1", "bases": bases, "fields": _fields_pydantic_v1(cls)}
    fields = _fields_annotated(cls)
    if fields:
        return {"kind": "annotated", "bases": bases, "fields": fields}
    # Compiled struct (PyO3) with no Python annotations: record public data attrs.
    attrs = sorted(a for a in dir(cls)
                   if not a.startswith("_") and not callable(getattr(cls, a, None)))
    return {"kind": "compiled", "bases": bases, "fields": {},
            "attributes": attrs}


def capture_models() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in sorted(set(DECLARED_MODELS)):
        obj, source = _resolve(name, MODEL_SEARCH)
        if obj is _MISSING or not isinstance(obj, type):
            result[name] = {"_present": False}
            continue
        rec = _safe(lambda c=obj: introspect_model(c))
        if isinstance(rec, dict):
            rec["_present"] = True
            rec["source"] = source
        result[name] = rec
    return result


# --------------------------------------------------------------------------- #
# Method / signature introspection.
# --------------------------------------------------------------------------- #
def _build_sig_str(params: list[dict[str, Any]], returns: str | None) -> str:
    parts: list[str] = []
    for p in params:
        seg, kind = p["name"], p["kind"]
        if kind == "VAR_POSITIONAL":
            seg = f"*{seg}"
        elif kind == "VAR_KEYWORD":
            seg = f"**{seg}"
        if p["annotation"] is not None:
            seg += f": {p['annotation']}"
        if p["default"] != "<empty>":
            seg += f" = {p['default']}"
        parts.append(seg)
    out = "(" + ", ".join(parts) + ")"
    if returns is not None:
        out += f" -> {returns}"
    return out


def introspect_callable(obj: Any) -> dict[str, Any]:
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return {"_present": True, "signature": None, "_no_signature": True}
    params: list[dict[str, Any]] = []
    for p in sig.parameters.values():
        annotation = None if p.annotation is _EMPTY else normalize_type(p.annotation)
        default = "<empty>" if p.default is _EMPTY else normalize_default(p.default)
        params.append({"name": p.name, "kind": p.kind.name,
                       "annotation": annotation, "default": default})
    returns = None if sig.return_annotation is _EMPTY else normalize_type(sig.return_annotation)
    return {"_present": True, "params": params, "returns": returns,
            "signature": _build_sig_str(params, returns)}


def _class_members(cls: type, declared: list[str]) -> dict[str, Any]:
    names = set(declared)
    for attr in dir(cls):
        if not attr.startswith("_"):
            try:
                if callable(getattr(cls, attr)):
                    names.add(attr)
            except Exception:  # noqa: BLE001
                continue
    out: dict[str, Any] = {}
    for name in sorted(names):
        static = inspect.getattr_static(cls, name, _MISSING)
        if static is _MISSING and not hasattr(cls, name):
            out[name] = {"_present": False}
            continue
        target = getattr(cls, name, static)
        kind = "method"
        if isinstance(static, staticmethod):
            kind = "staticmethod"
        elif isinstance(static, classmethod):
            kind = "classmethod"
        elif isinstance(static, property):
            kind, target = "property", static.fget
        rec = introspect_callable(target) if callable(target) else {"_present": True, "_no_signature": True}
        rec["kind"] = kind
        out[name] = rec
    return out


def capture_methods() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for logical, declared in DECLARED_METHODS.items():
        cls, source = _resolve(logical, CLIENT_SEARCH[logical])
        if cls is _MISSING or not isinstance(cls, type):
            result[logical] = {"_present": False}
            continue
        result[logical] = {"_present": True, "source": source,
                           "members": _safe(lambda c=cls, d=declared: _class_members(c, d))}
    return result


# --------------------------------------------------------------------------- #
# Config, exceptions, compiled layer, package layout.
# --------------------------------------------------------------------------- #
def capture_config() -> dict[str, Any]:
    import os
    mod, source = None, None
    for mod_name in CONFIG_SEARCH:
        try:
            mod = importlib.import_module(mod_name)
            source = mod_name
            break
        except Exception:  # noqa: BLE001
            mod = None
    names = set(DECLARED_SOL_CONFIG)
    if mod is not None:
        names.update(n for n in dir(mod) if n.startswith("SOL_"))
    defaults: dict[str, Any] = {}
    override = False
    for name in sorted(names):
        if name in os.environ:
            override = True
        val = getattr(mod, name, _MISSING) if mod is not None else _MISSING
        if val is _MISSING:
            defaults[name] = {"_present": False}
        else:
            defaults[name] = {"value": normalize_default(val), "type": type(val).__name__,
                              "env_var": name}
    present = mod is not None and any(v.get("_present", True) for v in defaults.values()
                                      if isinstance(v, dict))
    out: dict[str, Any] = {"_present": bool(present), "module": source, "defaults": defaults}
    if override:
        out["_env_override_detected"] = True
    return out


def capture_exceptions() -> dict[str, Any]:
    out: dict[str, Any] = {}
    declared = set(DECLARED_EXCEPTIONS)
    try:
        err_mod = importlib.import_module("shioaji.error")
        for name, obj in vars(err_mod).items():
            if isinstance(obj, type) and issubclass(obj, BaseException) and obj.__module__ == err_mod.__name__:
                declared.add(name)
    except Exception:  # noqa: BLE001
        pass
    for name in sorted(declared):
        obj, source = _resolve(name, EXCEPTION_SEARCH)
        if obj is _MISSING or not (isinstance(obj, type) and issubclass(obj, BaseException)):
            out[name] = {"_present": False}
            continue
        bases = sorted(_short(getattr(b, "__qualname__", b.__name__)) for b in obj.__bases__)
        out[name] = {"_present": True, "bases": bases, "source": source}
    return out


def _nm_symbols(mod: Any) -> dict[str, Any]:
    path = getattr(mod, "__file__", None)
    nm = shutil.which("nm")
    if not path or not path.endswith(".so") or nm is None:
        return {"_signal": "coarse", "_present": False}
    try:
        proc = subprocess.run([nm, "-D", "--defined-only", path],  # noqa: S603 - fixed argv
                              capture_output=True, text=True, timeout=30, check=False)
    except Exception as exc:  # noqa: BLE001
        return {"_signal": "coarse", "_present": False, "_error": type(exc).__name__}
    syms = sorted({line.split()[-1] for line in proc.stdout.splitlines() if line.strip()})
    return {"_signal": "coarse", "_present": True, "count": len(syms), "symbols": syms}


def capture_compiled() -> dict[str, Any]:
    result: dict[str, Any] = {}
    for mod_name, classes in DECLARED_COMPILED.items():
        try:
            mod = importlib.import_module(mod_name)
        except Exception as exc:  # noqa: BLE001
            result[mod_name] = {"_present": False, "_error": type(exc).__name__}
            continue
        entry: dict[str, Any] = {
            "_present": True,
            "module_attrs": sorted(n for n in dir(mod) if not n.startswith("_")),
            "classes": {},
        }
        for cls_name, methods in classes.items():
            cls = getattr(mod, cls_name, _MISSING)
            if cls is _MISSING or not isinstance(cls, type):
                entry["classes"][cls_name] = {"_present": False}
                continue
            method_recs: dict[str, Any] = {}
            for meth in sorted(methods):
                obj = getattr(cls, meth, _MISSING)
                if obj is _MISSING:
                    method_recs[meth] = {"_present": False}
                    continue
                rec = introspect_callable(obj)
                params = rec.get("params")
                rec["param_count"] = len(params) if params is not None else None
                method_recs[meth] = rec
            entry["classes"][cls_name] = {"_present": True, "methods": method_recs}
        entry["nm_symbols"] = _nm_symbols(mod)
        result[mod_name] = entry
    return result


def capture_package_layout() -> dict[str, Any]:
    """Record the structural shape (pure-Python vs compiled) — the headline of a
    rewrite like 1.3.x→1.5.x. ``is_compiled`` true means the SDK moved into a
    ``shioaji._core`` extension and the legacy submodule paths are shims."""
    submodules: dict[str, bool] = {}
    for name in LAYOUT_SUBMODULES:
        try:
            importlib.import_module(name)
            submodules[name] = True
        except Exception:  # noqa: BLE001
            submodules[name] = False
    top_exports: list[str] = []
    try:
        shioaji = importlib.import_module("shioaji")
        top_exports = sorted(n for n in dir(shioaji) if not n.startswith("_"))
    except Exception:  # noqa: BLE001
        pass
    return {
        "_present": True,
        "is_compiled": submodules.get("shioaji._core", False),
        "submodules": submodules,
        "top_level_export_count": len(top_exports),
        "top_level_exports": top_exports,
    }


# --------------------------------------------------------------------------- #
# Top-level assembly + deterministic serialization.
# --------------------------------------------------------------------------- #
def _safe(fn: typing.Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - isolate one section's failure
        return {"_present": False, "_error": type(exc).__name__}


def capture_dist() -> dict[str, Any]:
    import shioaji
    runtime_version = getattr(shioaji, "__version__", None)
    try:
        dist = ilm.distribution("shioaji")
        meta_version, requires = dist.version, sorted(dist.requires or [])
    except ilm.PackageNotFoundError:
        meta_version, requires = None, []
    out: dict[str, Any] = {"_present": True, "name": "shioaji", "version": runtime_version,
                           "metadata_version": meta_version, "requires": requires}
    if meta_version and runtime_version and meta_version != runtime_version:
        out["_version_mismatch"] = True
    return out


def build_surface_snapshot() -> dict[str, Any]:
    # Deprecation shims in shioaji ≥1.5 warn on every legacy attribute access; we
    # resolve those paths deliberately. Contain the suppression to this call so
    # importing the module (e.g. from the CI guard) never mutates the global
    # warnings filter for the rest of the test suite.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return _build_surface_snapshot_inner()


def _build_surface_snapshot_inner() -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "capture": {
            "tool_version": TOOL_VERSION,
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "pydantic_runtime": _pydantic_runtime(),
        },
        "dist": _safe(capture_dist),
        "package_layout": _safe(capture_package_layout),
        "constants": _safe(capture_constants),
        "models": _safe(capture_models),
        "methods": _safe(capture_methods),
        "config": _safe(capture_config),
        "exceptions": _safe(capture_exceptions),
        "compiled": _safe(capture_compiled),
    }
    body["snapshot_sha256"] = _digest(body)
    return body


def _pydantic_runtime() -> str:
    try:
        import pydantic
        return (pydantic.VERSION or "?").split(".", 1)[0]
    except Exception:  # noqa: BLE001
        return "none"


def canonical_json(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def _digest(body: dict[str, Any]) -> str:
    payload = json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture the installed shioaji API surface.")
    parser.add_argument("--emit-json", action="store_true",
                        help="Write the canonical JSON snapshot to stdout.")
    parser.parse_args(argv)
    sys.stdout.write(canonical_json(build_surface_snapshot()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
