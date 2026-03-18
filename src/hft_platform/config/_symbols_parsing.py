"""Low-level parsing helpers for the symbols subsystem.

Handles value parsing (booleans, numerics, ranges, lists),
filter-token interpretation, KV-token and CSV-spec parsing.
"""

from __future__ import annotations

import re
from typing import Any

from structlog import get_logger

from hft_platform.config._symbols_types import (
    FILTER_BOOL_KEYS,
    FILTER_KEYS,
    FILTER_LIST_KEYS,
    FilterSpec,
    SymbolBuildResult,
)

logger = get_logger("config.symbols.parsing")


# ---------------------------------------------------------------------------
# Primitive value parsers
# ---------------------------------------------------------------------------


def parse_bool_value(raw: str) -> bool | None:
    """Parse a boolean from common truthy/falsy string representations."""
    text = str(raw or "").strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return None


def parse_list_value(raw: str) -> list[str]:
    """Split a pipe- or comma-delimited string into stripped tokens."""
    return [item.strip() for item in re.split(r"[|,]", str(raw or "")) if item.strip()]


def parse_numeric_value(raw: str) -> float | None:
    """Convert a numeric string (with optional ``%`` / commas) to *float*."""
    text = str(raw or "").strip().replace(",", "")
    if not text:
        return None
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return None


def parse_range_value(raw: str) -> tuple[float, float] | None:
    """Parse ``low..high`` or ``low-high`` into *(low, high)*."""
    text = str(raw or "").strip()
    if ".." in text:
        parts = text.split("..", 1)
    elif "-" in text and not text.startswith("-") and text.count("-") == 1:
        parts = text.split("-", 1)
    else:
        return None
    low = parse_numeric_value(parts[0])
    high = parse_numeric_value(parts[1])
    if low is None or high is None:
        return None
    return low, high


def normalize_month_token(raw: str) -> str:
    """Normalize month tokens (front/near/next/far) to lowercase."""
    return str(raw or "").strip().lower()


def normalize_tags(raw: Any) -> list[str]:
    """Normalize a raw tag value into a list of non-empty strings."""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[|,]", raw)
    elif isinstance(raw, (list, tuple, set)):
        parts = list(raw)
    else:
        parts = [str(raw)]
    cleaned: list[str] = []
    for item in parts:
        tag = str(item).strip()
        if tag:
            cleaned.append(tag)
    return cleaned


def merge_tags(*tag_sets: Any) -> list[str]:
    """Merge multiple tag iterables, deduplicating case-insensitively."""
    seen: set[str] = set()
    merged: list[str] = []
    for tags in tag_sets:
        for tag in tags:
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(tag)
    return merged


# ---------------------------------------------------------------------------
# Filter-token helpers
# ---------------------------------------------------------------------------


def looks_like_filter(token: str) -> bool:
    """Return *True* if *token* looks like a filter expression."""
    raw = str(token or "").strip()
    if raw.startswith("@"):
        raw = raw[1:]
    raw = raw.strip()
    if not raw:
        return False
    if raw.lower() == "roll":
        return False
    for op in (">=", "<=", ">", "<", "="):
        if op in raw:
            key = raw.split(op, 1)[0].strip().lower()
            return key in FILTER_KEYS
    return False


def _merge_numeric_bounds(filters: FilterSpec, key: str, low: float | None, high: float | None) -> None:
    if low is not None:
        prev = filters.numeric_min.get(key)
        filters.numeric_min[key] = low if prev is None else max(prev, low)
    if high is not None:
        prev = filters.numeric_max.get(key)
        filters.numeric_max[key] = high if prev is None else min(prev, high)


def parse_filter_token(token: str, filters: FilterSpec, result: SymbolBuildResult, context: str) -> bool:
    """Parse a single filter token into *filters*.  Returns *True* if consumed."""
    raw = str(token or "").strip()
    if raw.startswith("@"):
        raw = raw[1:]
    raw = raw.strip()
    if not raw:
        return False

    if raw.lower() == "roll":
        filters.roll = True
        return True

    op_found = None
    for op in (">=", "<=", ">", "<", "="):
        if op in raw:
            op_found = op
            break
    if not op_found:
        return False

    key, value = raw.split(op_found, 1)
    key = key.strip().lower()
    value = value.strip()
    if key not in FILTER_KEYS:
        return False

    if key in FILTER_LIST_KEYS and op_found == "=":
        items = parse_list_value(value)
        if key == "exclude":
            filters.exclude_flags.update({item.lower() for item in items})
        elif key == "month":
            filters.merge_months([normalize_month_token(item) for item in items])
        else:
            filters.enums.setdefault(key, set()).update({item.lower() for item in items})
        return True

    if key in FILTER_BOOL_KEYS and op_found == "=":
        flag = parse_bool_value(value)
        if flag is None:
            result.warnings.append(f"Invalid boolean filter in {context}: {token}")
            return True
        filters.bools[key] = flag
        return True

    if op_found == "=" and value.lower().startswith("top"):
        num = value[3:]
        if num.isdigit():
            filters.top_n[key] = int(num)
            return True

    range_val = parse_range_value(value)
    if range_val is not None:
        _merge_numeric_bounds(filters, key, range_val[0], range_val[1])
        return True

    num_val = parse_numeric_value(value)
    if num_val is None:
        result.warnings.append(f"Invalid numeric filter in {context}: {token}")
        return True

    if key == "exclude_dte":
        if op_found in {"<", "<="}:
            filters.exclude_dte_max = int(num_val)
        else:
            result.warnings.append(f"exclude_dte expects <= in {context}: {token}")
        return True

    if key == "dte" and filters.roll:
        if op_found in {"<", "<="}:
            filters.roll_dte_max = int(num_val)
        else:
            result.warnings.append(f"roll dte expects <= in {context}: {token}")
        return True

    if op_found in {">", ">="}:
        _merge_numeric_bounds(filters, key, num_val, None)
    elif op_found in {"<", "<="}:
        _merge_numeric_bounds(filters, key, None, num_val)
    else:
        _merge_numeric_bounds(filters, key, num_val, num_val)
    return True


# ---------------------------------------------------------------------------
# KV-token and CSV-spec parsing
# ---------------------------------------------------------------------------


def parse_kv_tokens(tokens: list[str]) -> dict[str, Any]:
    """Parse ``key=value`` tokens into an attribute dict."""
    attrs: dict[str, Any] = {}
    for token in tokens:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if not value:
            continue
        if key in {"exchange", "exch"}:
            attrs["exchange"] = value
        elif key in {"product_type", "security_type", "type"}:
            attrs["product_type"] = value
        elif key in {"tick", "tick_size"}:
            try:
                attrs["tick_size"] = float(value)
            except ValueError:
                attrs.setdefault("_invalid", []).append(f"tick_size={value}")
        elif key in {"price_scale", "scale"}:
            try:
                attrs["price_scale"] = int(value)
            except ValueError:
                attrs.setdefault("_invalid", []).append(f"price_scale={value}")
        elif key in {"order_cond", "order_condition"}:
            attrs["order_cond"] = value
        elif key in {"order_lot", "lot"}:
            attrs["order_lot"] = value
        elif key in {"oc_type", "octype"}:
            attrs["oc_type"] = value
        elif key in {"account"}:
            attrs["account"] = value
        elif key in {"tags", "tag"}:
            attrs["tags"] = normalize_tags(value)
        elif key in {"name", "contract_name"}:
            attrs["name"] = value
        elif key in {"contract_size", "size"}:
            try:
                attrs["contract_size"] = float(value)
            except ValueError:
                attrs.setdefault("_invalid", []).append(f"contract_size={value}")
    return attrs


def parse_csv_spec(spec: str) -> tuple[str, dict[str, Any]]:
    """Parse a CSV symbol spec: ``code,exchange,tick_size,price_scale,tags``."""
    fields = [f.strip() for f in spec.split(",")]
    fields = [f for f in fields if f]
    if not fields:
        return "", {}
    attrs: dict[str, Any] = {}
    if len(fields) > 1:
        attrs["exchange"] = fields[1]
    if len(fields) > 2:
        try:
            attrs["tick_size"] = float(fields[2])
        except ValueError:
            attrs.setdefault("_invalid", []).append(f"tick_size={fields[2]}")
    if len(fields) > 3:
        try:
            attrs["price_scale"] = int(fields[3])
        except ValueError:
            attrs.setdefault("_invalid", []).append(f"price_scale={fields[3]}")
    if len(fields) > 4:
        attrs["tags"] = normalize_tags(fields[4])
    return fields[0], attrs


def parse_attrs_and_filters(
    tokens: list[str], result: SymbolBuildResult, context: str
) -> tuple[dict[str, Any], FilterSpec]:
    """Split a list of tokens into attribute-dict + filter-spec."""
    attrs: dict[str, Any] = {}
    filters = FilterSpec()
    for token in tokens:
        if parse_filter_token(token, filters, result, context):
            continue
        if "=" in token:
            attrs.update(parse_kv_tokens([token]))
            continue
        result.warnings.append(f"Unknown token in {context}: {token}")
    return attrs, filters
