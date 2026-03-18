"""Spec expansion logic for the symbols subsystem.

Handles futures, options, and synthetic symbol expansion from DSL specs,
including month selection, ATM/OTM strike picking, and roll logic.
"""

from __future__ import annotations

import re
from typing import Any

from structlog import get_logger

from hft_platform.config._symbols_filters import apply_filters
from hft_platform.config._symbols_parsing import (
    looks_like_filter,
    merge_tags,
    parse_filter_token,
)
from hft_platform.config._symbols_types import (
    PLUS_MINUS,
    ContractIndex,
    FilterSpec,
    SymbolBuildResult,
    contract_dte_days,
    expiry_key,
)

logger = get_logger("config.symbols.expansion")


# ---------------------------------------------------------------------------
# Entry builder
# ---------------------------------------------------------------------------


def _default_exchange_for_code(code: str) -> str:
    if code.isdigit():
        return "TSE"
    return "FUT"


def build_entry(
    code: str,
    attrs: dict[str, Any],
    contract: dict[str, Any] | None,
    result: SymbolBuildResult,
    extra_tags: list[str] | None = None,
) -> dict[str, Any] | None:
    """Build a single symbol entry dict from *code*, *attrs*, and *contract*."""
    if not code:
        return None

    entry: dict[str, Any] = {"code": code}
    if contract:
        for key in ("name", "exchange", "tick_size", "price_scale", "contract_size"):
            if key in contract and contract[key] is not None:
                entry[key] = contract[key]
        if "product_type" not in entry:
            c_type = contract.get("type") or contract.get("security_type")
            if c_type:
                entry["product_type"] = c_type

    entry.update({k: v for k, v in attrs.items() if v is not None})

    if "exchange" not in entry or not entry["exchange"]:
        entry["exchange"] = _default_exchange_for_code(code)
        result.warnings.append(f"Defaulted exchange for {code} to {entry['exchange']}")

    tags = merge_tags(entry.get("tags", []), extra_tags or [])
    if tags:
        entry["tags"] = tags

    return entry


# ---------------------------------------------------------------------------
# Grouping helpers
# ---------------------------------------------------------------------------


def _group_by_expiry(contracts: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for contract in contracts:
        grouped.setdefault(expiry_key(contract), []).append(contract)
    return [grouped[key] for key in sorted(grouped.keys())]


# ---------------------------------------------------------------------------
# Synthetic expansion
# ---------------------------------------------------------------------------


def _expand_synthetic(prefix: str, count: int, attrs: dict[str, Any], result: SymbolBuildResult) -> None:
    if count <= 0:
        result.errors.append(f"Synthetic count must be positive: {count}")
        return
    tags = ["synthetic", "stress"]
    for i in range(1, count + 1):
        code = f"{prefix.upper()}{i:04d}"
        entry = build_entry(code, attrs, None, result, extra_tags=tags)
        if entry:
            entry.setdefault("exchange", "SIM")
            result.symbols.append(entry)


# ---------------------------------------------------------------------------
# Futures expansion
# ---------------------------------------------------------------------------


def _expand_futures(
    root: str,
    month_token: str,
    attrs: dict[str, Any],
    contract_index: ContractIndex | None,
    result: SymbolBuildResult,
    filters: FilterSpec | None = None,
) -> None:
    if filters is None:
        filters = FilterSpec()
    if not contract_index:
        result.errors.append(f"Futures rule requires contract cache: {root}@{month_token}")
        return

    contracts = contract_index.futures_by_root.get(root)
    if not contracts:
        result.errors.append(f"No futures contracts found for root {root}")
        return
    contracts = [
        c
        for c in contracts
        if not str(c.get("code", "")).endswith(("R1", "R2")) and not str(c.get("symbol", "")).endswith(("R1", "R2"))
    ]
    if not contracts:
        result.errors.append(f"No futures contracts found for root {root} after filtering R1/R2")
        return

    groups = _group_by_expiry(contracts)
    idx_map = {"front": 0, "near": 0, "next": 1, "far": 2}
    month_indices: list[int] = []
    month_labels: list[str] = []

    if filters.roll or str(month_token).lower() == "roll":
        threshold = filters.roll_dte_max if filters.roll_dte_max is not None else 5
        front_group = groups[0] if groups else []
        front_dte = contract_dte_days(front_group[0]) if front_group else None
        idx = 0
        label = "front"
        if front_dte is not None and front_dte <= threshold and len(groups) > 1:
            idx = 1
            label = "next"
        month_indices = [idx]
        month_labels = [label]
    else:
        tokens = filters.months if filters.months is not None else [month_token]
        for token in tokens:
            month = str(token).lower()
            idx_val = idx_map.get(month)
            if idx_val is None:
                result.errors.append(f"Unknown futures month selector: {token} ({root})")
                continue
            if idx_val >= len(groups):
                result.errors.append(f"Futures month selector out of range: {root}@{token}")
                continue
            month_indices.append(idx_val)
            month_labels.append(month)

    for idx, label in zip(month_indices, month_labels):
        group = groups[idx]
        selected = sorted(group, key=lambda c: str(c.get("code")))
        selected = apply_filters(selected, filters, result, contract_index, context=f"{root}@{label}")
        if not selected:
            continue
        contract = selected[0]
        tags = ["futures", f"{label}_month", root.lower()]
        entry = build_entry(str(contract.get("code")), attrs, contract, result, extra_tags=tags)
        if entry:
            entry.setdefault("exchange", "FUT")
            result.symbols.append(entry)


# ---------------------------------------------------------------------------
# Options helpers
# ---------------------------------------------------------------------------


def _normalize_option_right(value: Any) -> str:
    text = str(value or "").upper()
    if "CALL" in text or text.endswith("C"):
        return "C"
    if "PUT" in text or text.endswith("P"):
        return "P"
    return ""


def _pick_reference_price(contracts: list[dict[str, Any]]) -> float | None:
    for key in ("reference", "reference_price", "underlying_price", "close"):
        for contract in contracts:
            value = contract.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def _parse_selector(selector: str) -> tuple[str, int]:
    raw = selector.upper().replace("+/-", "+-").replace(PLUS_MINUS, "+-")
    if raw.startswith("ATM"):
        mode = "ATM"
    elif raw.startswith("OTM"):
        mode = "OTM"
    else:
        return "UNKNOWN", 0

    offset = 0
    match = re.search(r"[+-](\d+)", raw)
    if match:
        offset = int(match.group(1))
    elif "+-" in raw:
        match = re.search(r"\+-(\d+)", raw)
        if match:
            offset = int(match.group(1))
    return mode, offset


# ---------------------------------------------------------------------------
# Options expansion
# ---------------------------------------------------------------------------


def _expand_options(
    root: str,
    month_token: str,
    selector: str,
    attrs: dict[str, Any],
    contract_index: ContractIndex | None,
    result: SymbolBuildResult,
    filters: FilterSpec | None = None,
) -> None:
    if filters is None:
        filters = FilterSpec()
    if not contract_index:
        result.errors.append(f"Option rule requires contract cache: OPT@{root}@{month_token}@{selector}")
        return

    contracts = contract_index.options_by_root.get(root)
    if not contracts:
        result.errors.append(f"No option contracts found for root {root}")
        return

    groups = _group_by_expiry(contracts)
    idx_map = {"front": 0, "near": 0, "next": 1, "far": 2}
    tokens = filters.months if filters.months is not None else [month_token]

    for token in tokens:
        month = str(token).lower()
        idx = idx_map.get(month)
        if idx is None:
            result.errors.append(f"Unknown options month selector: {token} ({root})")
            continue
        if idx >= len(groups):
            result.errors.append(f"Options month selector out of range: {root}@{token}")
            continue

        group = groups[idx]
        strike_values: set[float] = set()
        for contract in group:
            raw = contract.get("strike")
            if raw is None:
                raw = contract.get("strike_price")
            if raw is None:
                continue
            try:
                strike_values.add(float(raw))
            except (TypeError, ValueError):
                continue
        strikes = sorted(strike_values)
        if not strikes:
            result.errors.append(f"No strike data for options root {root}")
            return

        reference = _pick_reference_price(group)
        if reference is None:
            reference = strikes[len(strikes) // 2]
            result.warnings.append(f"Using median strike for ATM ({root} {month})")

        atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - reference))

        mode, offset = _parse_selector(selector)
        if mode == "UNKNOWN":
            result.errors.append(f"Unknown option selector: {selector} ({root})")
            return

        selected_strikes: set[float] = set()
        otm_call_strikes: set[float] = set()
        otm_put_strikes: set[float] = set()
        if mode == "ATM":
            start = max(atm_idx - offset, 0)
            end = min(atm_idx + offset, len(strikes) - 1)
            for i in range(start, end + 1):
                selected_strikes.add(strikes[i])
        else:
            for i in range(1, offset + 1):
                if atm_idx + i < len(strikes):
                    otm_call_strikes.add(strikes[atm_idx + i])
                if atm_idx - i >= 0:
                    otm_put_strikes.add(strikes[atm_idx - i])
            selected_strikes = otm_call_strikes | otm_put_strikes

        if not selected_strikes:
            result.errors.append(f"Option selector produced empty set: {selector} ({root})")
            return

        mode_tag = "atm" if mode == "ATM" else "otm"
        tags = ["options", f"{month}_month", mode_tag, root.lower()]

        candidates: list[dict[str, Any]] = []
        for contract in group:
            strike = contract.get("strike")
            if strike is None:
                strike = contract.get("strike_price")
            if strike is not None:
                try:
                    strike = float(strike)
                except (TypeError, ValueError):
                    strike = None
            if strike not in selected_strikes:
                continue
            right = _normalize_option_right(contract.get("right") or contract.get("option_right"))
            if mode == "OTM":
                if right == "C" and strike not in otm_call_strikes:
                    continue
                if right == "P" and strike not in otm_put_strikes:
                    continue
            if right not in {"C", "P"}:
                continue
            candidates.append(contract)

        candidates = apply_filters(
            candidates,
            filters,
            result,
            contract_index,
            context=f"{root}@{month}",
            reference=reference,
        )
        for contract in candidates:
            entry = build_entry(str(contract.get("code")), attrs, contract, result, extra_tags=tags)
            if entry:
                entry.setdefault("exchange", "OPT")
                result.symbols.append(entry)


# ---------------------------------------------------------------------------
# Top-level spec dispatch
# ---------------------------------------------------------------------------


def expand_spec(
    spec: str,
    attrs: dict[str, Any],
    contract_index: ContractIndex | None,
    result: SymbolBuildResult,
    filters: FilterSpec | None = None,
) -> None:
    """Route a DSL spec to the appropriate expander (futures/options/synthetic/literal)."""
    if filters is None:
        filters = FilterSpec()
    if "@" not in spec:
        contract = contract_index.by_code.get(spec) if contract_index else None
        candidates = [contract or {"code": spec}]
        filtered = apply_filters(candidates, filters, result, contract_index, context=spec)
        if not filtered:
            return
        entry = build_entry(spec, attrs, filtered[0] if filtered else contract, result)
        if entry:
            result.symbols.append(entry)
        return

    parts = [p for p in spec.split("@") if p]
    if not parts:
        result.errors.append(f"Invalid rule spec: {spec}")
        return

    head = parts[0].upper()
    if head in {"OPT", "OPTION", "OPTIONS"}:
        if len(parts) < 2:
            result.errors.append(f"Option rule missing root: {spec}")
            return
        root = parts[1].upper()
        idx = 2
        month = "near"
        selector = "ATM"
        if idx < len(parts) and not looks_like_filter(parts[idx]):
            month = parts[idx]
            idx += 1
        if idx < len(parts) and not looks_like_filter(parts[idx]):
            selector = parts[idx]
            idx += 1
        for token in parts[idx:]:
            parse_filter_token(token, filters, result, spec)
        _expand_options(root, month, selector, attrs, contract_index, result, filters)
        return

    if head in {"SYNTH", "STRESS"}:
        if len(parts) < 2:
            result.errors.append(f"Synthetic rule missing count: {spec}")
            return
        try:
            count = int(parts[1])
        except ValueError:
            result.errors.append(f"Invalid synthetic count: {spec}")
            return
        _expand_synthetic(head.lower(), count, attrs, result)
        return

    if head in {"FUT", "FUTURES"}:
        if len(parts) < 2:
            result.errors.append(f"Futures rule missing root: {spec}")
            return
        root = parts[1].upper()
        idx = 2
        month = "front"
        if idx < len(parts) and not looks_like_filter(parts[idx]):
            month = parts[idx]
            idx += 1
        if str(month).lower() == "roll":
            filters.roll = True
        for token in parts[idx:]:
            parse_filter_token(token, filters, result, spec)
        _expand_futures(root, month, attrs, contract_index, result, filters)
        return

    if len(parts) < 2:
        result.errors.append(f"Unknown rule spec: {spec}")
        return

    root = parts[0].upper()
    idx = 1
    month = "front"
    if idx < len(parts) and not looks_like_filter(parts[idx]):
        month = parts[idx]
        idx += 1
    if str(month).lower() == "roll":
        filters.roll = True
    for token in parts[idx:]:
        parse_filter_token(token, filters, result, spec)
    _expand_futures(root, month, attrs, contract_index, result, filters)
