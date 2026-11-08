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


def _filter_active_contracts(
    contracts: list[dict[str, Any]],
    result: SymbolBuildResult,
    context: str,
    as_of: Any = None,
) -> list[dict[str, Any]]:
    """Drop derivatives whose delivery_date is strictly in the past.

    Contracts without a parseable expiry (e.g., perpetuals, malformed cache rows)
    are kept so a broken cache doesn't silently delete the universe.
    """
    active: list[dict[str, Any]] = []
    expired: list[str] = []
    for c in contracts:
        dte = contract_dte_days(c, as_of)
        if dte is None:
            active.append(c)
            continue
        if dte < 0:
            code = str(c.get("code") or "?")
            expired.append(code)
            continue
        active.append(c)
    if expired:
        result.warnings.append(
            f"Dropped {len(expired)} expired contracts ({context}): "
            f"{', '.join(expired[:5])}{'...' if len(expired) > 5 else ''}"
        )
    return active


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

    _product = str(entry.get("product_type") or "").lower()
    if _product in {"option", "opt", "options"} and contract:
        _enrich_option_entry(entry, contract, code, result)

    if "exchange" not in entry or not entry["exchange"]:
        entry["exchange"] = _default_exchange_for_code(code)
        result.warnings.append(f"Defaulted exchange for {code} to {entry['exchange']}")

    tags = merge_tags(entry.get("tags", []), extra_tags or [])
    if tags:
        entry["tags"] = tags

    return entry


# ---------------------------------------------------------------------------
# Option enrichment helpers
# ---------------------------------------------------------------------------

# TAIEX weekly option roots as the broker lists them: Wednesday weeklies TX1/TX2/TX4/TX5 (the third Wednesday is the
# monthly TXO, so TX3 is normally absent) and Friday weeklies TXU, TXV, ... TX1, TX2, TXU and TXV were seen in the
# 2026-08-31 contract cache; the rest are listed so a week that uses them is not silently dropped. A root that is not
# listed by the broker simply contributes no contracts.
TXO_WEEKLY_ROOTS: tuple[str, ...] = ("TX1", "TX2", "TX3", "TX4", "TX5", "TXU", "TXV", "TXW", "TXX", "TXY", "TXZ")

# A virtual option root that stands for several broker roots. ``OPT@TXO_WEEKLY@near`` is the nearest weekly expiry of
# any of them, so the rule follows the Wednesday and Friday weeklies without naming the week. It never includes the
# monthly TXO: the current month is its own rule (``OPT@TXO@near``).
OPTION_ROOT_FAMILIES: dict[str, tuple[str, ...]] = {"TXO_WEEKLY": TXO_WEEKLY_ROOTS}

# Map option root prefix → underlying futures root
_OPTION_UNDERLYING_MAP: dict[str, str] = {
    "TXO": "TX",
    **{weekly: "TX" for weekly in TXO_WEEKLY_ROOTS},
    "MXO": "MX",
    "NQO": "NQ",
    "XIO": "XI",
    "GTX": "TX",
}

# Default point value per option root (NTD per index point)
_OPTION_POINT_VALUE: dict[str, int] = {
    "TXO": 50,
    **{weekly: 50 for weekly in TXO_WEEKLY_ROOTS},
    "MXO": 10,
}

# Default trading metadata per futures root.
# Platform invariant: all price fields use price_scale=10000 (see test_futures_symbol_config).
# tick_size is in raw points; the scaled tick is tick_size * price_scale at the boundary.
_FUTURES_METADATA: dict[str, dict[str, int]] = {
    "TXF": {"point_value": 200, "tick_size": 1, "price_scale": 10000},
    "MXF": {"point_value": 50, "tick_size": 1, "price_scale": 10000},
    "TMF": {"point_value": 10, "tick_size": 1, "price_scale": 10000},
    "EXF": {"point_value": 4000, "tick_size": 1, "price_scale": 10000},
    "FXF": {"point_value": 1000, "tick_size": 1, "price_scale": 10000},
    "GXF": {"point_value": 50, "tick_size": 1, "price_scale": 10000},
}


def _enrich_future_entry(entry: dict[str, Any], code: str) -> None:
    """Populate futures trading metadata (point_value/tick_size/price_scale) by root prefix."""
    root = code[:3].upper()
    meta = _FUTURES_METADATA.get(root)
    if not meta:
        return
    for key, value in meta.items():
        entry.setdefault(key, value)


def _enrich_option_entry(
    entry: dict[str, Any],
    contract: dict[str, Any],
    code: str,
    result: SymbolBuildResult,
) -> None:
    """Populate option-specific metadata fields into *entry* in-place.

    Fields set (using setdefault where attrs may have already provided values):
        right, strike, expiry, underlying, point_value, price_scale
    """
    # right
    raw_right = contract.get("option_right") or contract.get("right")
    if raw_right is not None:
        entry["right"] = _normalize_option_right(raw_right)

    # strike — raw int from exchange, no scaling
    raw_strike = contract.get("strike_price")
    if raw_strike is None:
        raw_strike = contract.get("strike")
    if raw_strike is not None:
        entry["strike"] = raw_strike
    else:
        result.warnings.append(f"Missing strike for option {code}")

    # expiry
    raw_expiry = contract.get("delivery_date")
    if raw_expiry is None:
        raw_expiry = contract.get("expiry") or contract.get("expiry_date")
    if raw_expiry is not None:
        entry["expiry"] = raw_expiry
    else:
        result.warnings.append(f"Missing expiry for option {code}")

    # underlying — derive root prefix from code, look up in map
    matched_root: str | None = None
    for prefix in _OPTION_UNDERLYING_MAP:
        if code.upper().startswith(prefix):
            matched_root = prefix
            break
    if matched_root is None:
        # Try entry.get("root") if present (set by caller)
        matched_root = str(entry.get("root") or "")

    if matched_root:
        entry.setdefault("underlying", _OPTION_UNDERLYING_MAP.get(matched_root, matched_root))

    # point_value — attrs take precedence (already merged via entry.update(attrs)), use setdefault
    if matched_root and matched_root in _OPTION_POINT_VALUE:
        entry.setdefault("point_value", _OPTION_POINT_VALUE[matched_root])

    # price_scale — platform default 10000; use setdefault so attrs/contract override works
    entry.setdefault("price_scale", 10000)


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

    contracts = _filter_active_contracts(contracts, result, context=f"futures {root}", as_of=contract_index.as_of)
    if not contracts:
        result.errors.append(f"No active (non-expired) futures contracts for root {root}")
        return

    groups = _group_by_expiry(contracts)
    idx_map = {
        "front": 0,
        "near": 0,
        "next": 1,
        "far": 2,
        "far_plus_1": 3,
        "far_plus_2": 4,
        "far_plus_3": 5,
    }
    month_indices: list[int] = []
    month_labels: list[str] = []

    if filters.roll or str(month_token).lower() == "roll":
        threshold = filters.roll_dte_max if filters.roll_dte_max is not None else 5
        front_group = groups[0] if groups else []
        front_dte = contract_dte_days(front_group[0], contract_index.as_of) if front_group else None
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
                # Broker doesn't currently list this expiry (e.g., far months not yet listed).
                # Drop it from the universe with a warning instead of failing the build.
                result.warnings.append(
                    f"Futures month selector unavailable: {root}@{token} "
                    f"(only {len(groups)} active expiry/expiries in cache)"
                )
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
            _enrich_future_entry(entry, str(contract.get("code") or ""))
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


# Option root -> the future whose price the strike window must be centred on. Only roots whose underlying
# is certain are listed; anything else needs an explicit ``underlying_price``.
UNDERLYING_FUTURE_ROOT: dict[str, str] = {
    "TXO": "TXF",
    "TXO_WEEKLY": "TXF",
    **{weekly: "TXF" for weekly in TXO_WEEKLY_ROOTS},
}
# A reference price this many times above or below the chain's middle strike is a unit slip (x10, /10), not a market
# move; a real move, or a chain that has not caught up with it, stays well inside this band.
UNDERLYING_SCALE_FACTOR = 2.0


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def pick_underlying_price(
    root: str,
    group: list[dict[str, Any]],
    contract_index: ContractIndex,
) -> tuple[float | None, str]:
    """Price of the instrument an option chain is quoted on, with where it came from.

    An option contract's own ``reference`` / ``close`` is the option PREMIUM, not the underlying, so it is
    never used here. Order: an explicit ``underlying_price`` on the chain (contract or metrics), then the
    ``reference`` / ``underlying_price`` of the nearest non-expired underlying future.
    """
    for contract in group:
        code = str(contract.get("code") or "")
        for source in (contract, contract_index.metrics_by_code.get(code, {})):
            value = _positive_float(source.get("underlying_price"))
            if value is not None:
                return value, "underlying_price"

    future_root = UNDERLYING_FUTURE_ROOT.get(root.upper())
    futures = contract_index.futures_by_root.get(future_root, []) if future_root else []
    dated = [
        (dte, contract)
        for contract in futures
        if (dte := contract_dte_days(contract, contract_index.as_of)) is not None and dte >= 0
    ]
    if not dated:
        return None, ""
    nearest = min(dte for dte, _ in dated)
    # Only the nearest expiry is the underlying, but the continuous aliases (TXFR1/R2) share its date and the
    # real contract's cache order is not guaranteed, so every row at that expiry is tried.
    for _, contract in (item for item in dated if item[0] == nearest):
        code = str(contract.get("code") or "")
        for source in (contract_index.metrics_by_code.get(code, {}), contract):
            for key in ("underlying_price", "reference"):
                value = _positive_float(source.get(key))
                if value is not None:
                    return value, f"{future_root}:{code}"
    return None, ""


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


def underlying_on_chain_scale(reference: float, strikes: list[float]) -> bool:
    """True when ``reference`` is within ``UNDERLYING_SCALE_FACTOR`` of the chain's middle strike.

    Shared by the DSL expander and the connection pool's options roll, so a unit slip is judged the same way in both.
    """
    middle = strikes[len(strikes) // 2]
    return middle <= 0 or middle / UNDERLYING_SCALE_FACTOR <= reference <= middle * UNDERLYING_SCALE_FACTOR


def _vet_underlying(
    reference: float | None, source: str | None, strikes: list[float], label: str, result: SymbolBuildResult
) -> float | None:
    """Drop a reference whose scale is wrong; keep (and flag) one that is merely beyond the chain's edge.

    A unit slip would pin the window to an edge strike without a trace, so it is treated as no underlying and the
    strict / median paths decide. A price just past the last listed strike is a real market, so the window keeps
    clamping to the edge strike as before (replacing it with the median would select in-the-money options).
    """
    if reference is None:
        return None
    if not underlying_on_chain_scale(reference, strikes):
        result.warnings.append(
            f"Ignoring underlying {reference:g} from {source or 'chain'}: not within a factor of "
            f"{UNDERLYING_SCALE_FACTOR:g} of the {label} strikes {strikes[0]:g}-{strikes[-1]:g} (unit slip?)"
        )
        return None
    if not strikes[0] <= reference <= strikes[-1]:
        result.warnings.append(
            f"Underlying {reference:g} from {source or 'chain'} is beyond the {label} strike range "
            f"{strikes[0]:g}-{strikes[-1]:g}: the window is clamped to the edge strike"
        )
    return reference


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

    family = OPTION_ROOT_FAMILIES.get(root.upper())
    if family is not None:
        contracts = [c for member in family for c in contract_index.options_by_root.get(member, [])]
    else:
        contracts = contract_index.options_by_root.get(root) or []
    # A family with no listed weekly (a holiday week, or a cache refreshed before the next weekly is listed) must not
    # fail the whole build: the universe roll refuses a build with errors, which would also drop the monthly chain and
    # the futures. The family's expiry is skipped with a warning instead; a plain root keeps its error.
    report = result.warnings.append if family is not None else result.errors.append
    if not contracts:
        report(f"No option contracts found for root {root}")
        return

    contracts = _filter_active_contracts(contracts, result, context=f"options {root}", as_of=contract_index.as_of)
    if not contracts:
        report(f"No active (non-expired) option contracts for root {root}")
        return

    groups = _group_by_expiry(contracts)
    idx_map = {
        "front": 0,
        "near": 0,
        "next": 1,
        "far": 2,
        "far_plus_1": 3,
        "far_plus_2": 4,
        "far_plus_3": 5,
    }
    tokens = filters.months if filters.months is not None else [month_token]

    for token in tokens:
        month = str(token).lower()
        idx = idx_map.get(month)
        if idx is None:
            result.errors.append(f"Unknown options month selector: {token} ({root})")
            continue
        if idx >= len(groups):
            # Broker doesn't currently list this expiry; skip with warning instead of erroring out.
            result.warnings.append(
                f"Options month selector unavailable: {root}@{token} "
                f"(only {len(groups)} active expiry/expiries in cache)"
            )
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

        reference, source = pick_underlying_price(root, group, contract_index)
        reference = _vet_underlying(reference, source, strikes, f"{root} {month}", result)
        if reference is None:
            if contract_index.require_underlying:
                message = (
                    f"No underlying price to centre the {root} {month} option window on; refusing to guess. "
                    f"Supply underlying_price for the {root} chain or a reference for the nearest "
                    f"{UNDERLYING_FUTURE_ROOT.get(root.upper(), 'underlying')} future in the metrics file"
                )
                result.errors.append(message)
                # The universe roll may hold this chain's running window and still roll the rest; see
                # QuoteConnectionPool._hold_unpriced_option_windows.
                result.unpriced_option_errors.append(message)
                result.unpriced_option_roots.append(root.upper())
                return
            reference = strikes[len(strikes) // 2]
            result.warnings.append(
                f"Using median strike for ATM ({root} {month}): window is NOT centred on the underlying"
            )

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
