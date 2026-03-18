"""Filter application logic for the symbols subsystem.

Resolves metrics from contracts/cache, evaluates boolean/enum/numeric
filters, top-N ranking, exclude flags, and DTE constraints.
"""

from __future__ import annotations

import re
from typing import Any

from structlog import get_logger

from hft_platform.config._symbols_parsing import parse_bool_value, parse_numeric_value
from hft_platform.config._symbols_types import (
    FILTER_BOOL_KEYS,
    FILTER_KEYS,
    FILTER_LIST_KEYS,
    METRIC_ALIASES,
    ContractIndex,
    FilterSpec,
    SymbolBuildResult,
    contract_dte_days,
)

logger = get_logger("config.symbols.filters")


# ---------------------------------------------------------------------------
# Metric resolution
# ---------------------------------------------------------------------------


def _coerce_numeric(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return parse_numeric_value(value)


def resolve_metric(
    contract: dict[str, Any],
    metrics_by_code: dict[str, dict[str, Any]],
    key: str,
    reference: float | None = None,
) -> Any | None:
    """Resolve a single metric *key* for *contract*."""
    code = str(contract.get("code") or "")
    metrics = metrics_by_code.get(code, {}) if metrics_by_code else {}

    if key == "dte":
        return contract_dte_days(contract)

    if key == "moneyness":
        strike = contract.get("strike")
        if strike is None:
            strike = contract.get("strike_price")
        strike_val = _coerce_numeric(strike)
        ref_val = reference
        if ref_val is None:
            ref_val = _coerce_numeric(metrics.get("underlying_price") or metrics.get("reference"))
        if strike_val is None or ref_val is None:
            return None
        if ref_val == 0:
            return None
        return strike_val / ref_val

    aliases = METRIC_ALIASES.get(key, (key,))
    value = None
    for alias in aliases:
        if alias in metrics:
            value = metrics.get(alias)
            break
    if value is None and key in contract:
        value = contract.get(key)

    if key in FILTER_BOOL_KEYS:
        if isinstance(value, bool):
            return value
        if value is None:
            return None
        return parse_bool_value(str(value))

    if key in FILTER_LIST_KEYS and key != "exclude":
        if value is None:
            return None
        if isinstance(value, (list, tuple, set)):
            return {str(v).strip().lower() for v in value if str(v).strip()}
        return {str(value).strip().lower()}

    if key in FILTER_KEYS:
        return _coerce_numeric(value)

    return value


# ---------------------------------------------------------------------------
# Exclude-flag helper
# ---------------------------------------------------------------------------


def _has_exclude_flag(metrics: dict[str, Any], flags: set[str]) -> bool:
    if not metrics:
        return False
    for flag in flags:
        if flag in metrics and bool(metrics.get(flag)):
            return True
        if metrics.get("flags"):
            raw_flags = metrics.get("flags")
            if isinstance(raw_flags, (list, tuple, set)):
                if flag in {str(v).strip().lower() for v in raw_flags}:
                    return True
            elif isinstance(raw_flags, str):
                if flag in {v.strip().lower() for v in re.split(r"[|,]", raw_flags)}:
                    return True
        key = f"is_{flag}"
        if key in metrics and bool(metrics.get(key)):
            return True
    return False


# ---------------------------------------------------------------------------
# Filter predicate helpers
# ---------------------------------------------------------------------------


def filters_active(filters: FilterSpec) -> bool:
    """Return *True* when any filter criterion is set."""
    if filters.bools or filters.enums or filters.numeric_min or filters.numeric_max:
        return True
    if filters.top_n or filters.exclude_flags:
        return True
    if filters.months or filters.roll or filters.roll_dte_max is not None:
        return True
    if filters.exclude_dte_max is not None:
        return True
    return False


# ---------------------------------------------------------------------------
# Main filter application
# ---------------------------------------------------------------------------


def apply_filters(
    contracts: list[dict[str, Any]],
    filters: FilterSpec,
    result: SymbolBuildResult,
    contract_index: ContractIndex | None,
    context: str,
    reference: float | None = None,
) -> list[dict[str, Any]]:
    """Apply *filters* to *contracts* and return the survivors."""
    if not contracts or not filters_active(filters):
        return contracts

    metrics_by_code = contract_index.metrics_by_code if contract_index else {}

    top_sets: dict[str, set[str]] = {}
    for key, limit in filters.top_n.items():
        ranked: list[tuple[Any, str]] = []
        for contract in contracts:
            code = str(contract.get("code") or "")
            value = resolve_metric(contract, metrics_by_code, key, reference)
            if value is None:
                continue
            ranked.append((value, code))
        if not ranked:
            result.errors.append(f"Filter {key}=top{limit} requires metrics: {context}")
            return []
        ranked.sort(key=lambda item: item[0], reverse=True)
        top_sets[key] = {code for _, code in ranked[:limit]}

    total = len(contracts)
    missing_counts: dict[str, int] = {}
    filtered: list[dict[str, Any]] = []

    numeric_keys = set(filters.numeric_min) | set(filters.numeric_max)

    for contract in contracts:
        code = str(contract.get("code") or "")
        metrics = metrics_by_code.get(code, {})
        keep = True

        for key, allowed_codes in top_sets.items():
            if code not in allowed_codes:
                keep = False
                break
        if not keep:
            continue

        for key, expected in filters.bools.items():
            val = resolve_metric(contract, metrics_by_code, key, reference)
            if val is None:
                missing_counts[key] = missing_counts.get(key, 0) + 1
                keep = False
                break
            if bool(val) != expected:
                keep = False
                break
        if not keep:
            continue

        for key, allowed in filters.enums.items():
            val = resolve_metric(contract, metrics_by_code, key, reference)
            if val is None:
                missing_counts[key] = missing_counts.get(key, 0) + 1
                keep = False
                break
            if isinstance(val, set):
                if not (val & allowed):
                    keep = False
                    break
            else:
                if str(val).strip().lower() not in allowed:
                    keep = False
                    break
        if not keep:
            continue

        for key in numeric_keys:
            val = resolve_metric(contract, metrics_by_code, key, reference)
            if val is None:
                missing_counts[key] = missing_counts.get(key, 0) + 1
                keep = False
                break
            min_val = filters.numeric_min.get(key)
            max_val = filters.numeric_max.get(key)
            if min_val is not None and val < min_val:
                keep = False
                break
            if max_val is not None and val > max_val:
                keep = False
                break
        if not keep:
            continue

        if filters.exclude_dte_max is not None:
            dte = resolve_metric(contract, metrics_by_code, "dte", reference)
            if dte is None:
                missing_counts["exclude_dte"] = missing_counts.get("exclude_dte", 0) + 1
                keep = False
            elif dte <= filters.exclude_dte_max:
                keep = False
        if not keep:
            continue

        if filters.exclude_flags and _has_exclude_flag(metrics, filters.exclude_flags):
            keep = False
        if not keep:
            continue

        filtered.append(contract)

    for key, missing in missing_counts.items():
        if missing >= total:
            result.errors.append(f"Filter {key} requires metrics for {context}")

    return filtered
