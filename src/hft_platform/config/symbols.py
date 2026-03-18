"""Symbol management facade — public API.

All public names remain importable from ``hft_platform.config.symbols``.
Implementation is split across private helper modules:

- ``_symbols_types``      — dataclasses, constants, tiny pure helpers
- ``_symbols_parsing``    — value/filter/KV/CSV parsing
- ``_symbols_filters``    — metric resolution and filter application
- ``_symbols_expansion``  — futures/options/synthetic expansion
- ``_symbols_contracts``  — contract cache I/O, broker fetch, validation
"""

from __future__ import annotations

import os
from typing import Any

from hft_platform.config._symbols_contracts import (
    fetch_contracts_from_broker,
    load_contract_cache,
    load_metrics_cache,
    preview_lines,
    validate_symbols,
    write_contract_cache,
    write_symbols_yaml,
)
from hft_platform.config._symbols_expansion import expand_spec
from hft_platform.config._symbols_parsing import (
    parse_attrs_and_filters,
    parse_csv_spec,
)
from hft_platform.config._symbols_types import (
    DEFAULT_CONTRACT_CACHE,
    DEFAULT_LIST_PATH,
    DEFAULT_METRICS_CACHE,
    DEFAULT_METRICS_ENV,
    DEFAULT_OUTPUT_PATH,
    FILTER_BOOL_KEYS,
    FILTER_KEYS,
    FILTER_LIST_KEYS,
    METRIC_ALIASES,
    PLUS_MINUS,
    VALID_EXCHANGES,
    ContractIndex,
    FilterSpec,
    SymbolBuildResult,
    contract_dte_days,
    derive_root,
    expiry_key,
    parse_date_key,
)

# Re-export private-module names that were previously module-level here.
# This keeps ``from hft_platform.config.symbols import X`` working for all
# known call-sites (cli.py, contracts_runtime.py, wizard.py, tests).

__all__ = [
    # Types / dataclasses
    "SymbolBuildResult",
    "ContractIndex",
    "FilterSpec",
    # Constants
    "DEFAULT_LIST_PATH",
    "DEFAULT_OUTPUT_PATH",
    "DEFAULT_CONTRACT_CACHE",
    "DEFAULT_METRICS_CACHE",
    "DEFAULT_METRICS_ENV",
    "PLUS_MINUS",
    "VALID_EXCHANGES",
    "FILTER_KEYS",
    "FILTER_BOOL_KEYS",
    "FILTER_LIST_KEYS",
    "METRIC_ALIASES",
    # Functions
    "derive_root",
    "parse_date_key",
    "expiry_key",
    "contract_dte_days",
    "load_metrics_cache",
    "load_contract_cache",
    "write_contract_cache",
    "write_symbols_yaml",
    "parse_symbols_list",
    "build_symbols",
    "validate_symbols",
    "preview_lines",
    "fetch_contracts_from_broker",
]


# ---------------------------------------------------------------------------
# Parsing orchestrator (kept here because it wires parsing + expansion)
# ---------------------------------------------------------------------------


def _resolve_include(path: str, raw: str) -> str:
    parts = raw.split(maxsplit=1)
    if len(parts) < 2:
        return ""
    target = parts[1].strip()
    if not target:
        return ""
    if os.path.isabs(target):
        return target
    base = os.path.dirname(path)
    return os.path.normpath(os.path.join(base, target))


def parse_symbols_list(
    path: str,
    contract_index: ContractIndex | None = None,
    result: SymbolBuildResult | None = None,
    seen: set[str] | None = None,
) -> SymbolBuildResult:
    """Parse a ``symbols.list`` file into a :class:`SymbolBuildResult`."""
    if result is None:
        result = SymbolBuildResult()
    if seen is None:
        seen = set()

    if path in seen:
        result.errors.append(f"Cyclic include detected: {path}")
        return result
    seen.add(path)

    if not os.path.exists(path):
        result.errors.append(f"symbols.list not found: {path}")
        return result

    with open(path, "r") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue

            if line.startswith("@include") or line.startswith("include "):
                include_path = _resolve_include(path, line)
                if not include_path:
                    result.errors.append(f"Invalid include syntax in {path}: {line}")
                    continue
                parse_symbols_list(include_path, contract_index, result, seen)
                continue

            attrs: dict[str, Any] = {}
            filters = FilterSpec()
            spec = line

            if " " in line or "=" in line:
                tokens = line.split()
                spec_token = tokens[0]
                attrs, filters = parse_attrs_and_filters(tokens[1:], result, f"{path}: {line}")

                if "," in spec_token:
                    spec, csv_attrs = parse_csv_spec(spec_token)
                    attrs = {**csv_attrs, **attrs}
                else:
                    spec = spec_token
            elif "," in line:
                spec, attrs = parse_csv_spec(line)

            if attrs.get("_invalid"):
                for item in attrs.get("_invalid", []):
                    result.warnings.append(f"Invalid field in {path}: {line} ({item})")
                attrs.pop("_invalid", None)

            if not spec:
                result.warnings.append(f"Skipping empty spec in {path}: {line}")
                continue

            expand_spec(spec, attrs, contract_index, result, filters)

    return result


def build_symbols(
    list_path: str = DEFAULT_LIST_PATH,
    contract_index: ContractIndex | None = None,
) -> SymbolBuildResult:
    """Build and deduplicate symbols from a ``symbols.list`` file."""
    result = parse_symbols_list(list_path, contract_index)

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in result.symbols:
        code = str(entry.get("code") or "")
        if not code:
            result.errors.append("Symbol entry missing code")
            continue
        if code in seen:
            result.errors.append(f"Duplicate symbol code: {code}")
            continue
        seen.add(code)
        deduped.append(entry)

    result.symbols = deduped
    return result
