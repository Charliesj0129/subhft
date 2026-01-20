from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

import yaml
from structlog import get_logger

logger = get_logger("config.symbols")

DEFAULT_LIST_PATH = "config/symbols.list"
DEFAULT_OUTPUT_PATH = "config/symbols.yaml"
DEFAULT_CONTRACT_CACHE = "config/contracts.json"
PLUS_MINUS = "\u00b1"


@dataclass
class SymbolBuildResult:
    symbols: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.errors


@dataclass
class ContractIndex:
    contracts: list[dict[str, Any]]
    by_code: dict[str, dict[str, Any]] = field(default_factory=dict)
    futures_by_root: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    options_by_root: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for contract in self.contracts:
            code = str(contract.get("code") or "").strip()
            if not code:
                continue
            self.by_code[code] = contract

            root = (contract.get("root") or derive_root(code)).upper()
            kind = str(contract.get("type") or contract.get("security_type") or "").lower()
            if kind in {"future", "fut", "futures"}:
                self.futures_by_root.setdefault(root, []).append(contract)
            elif kind in {"option", "opt", "options"}:
                self.options_by_root.setdefault(root, []).append(contract)


VALID_EXCHANGES = {"TSE", "OTC", "OES", "FUT", "FUTURES", "OPT", "OPTIONS", "TAIFEX", "IDX", "INDEX", "SIM"}


def derive_root(code: str) -> str:
    match = re.match(r"([A-Za-z]+)", code)
    return match.group(1) if match else code


def _parse_date_key(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        digits = str(int(value))
    else:
        digits = re.sub(r"[^0-9]", "", str(value))
    if len(digits) >= 8:
        return int(digits[:8])
    if len(digits) == 6:
        return int(digits) * 100
    return None


def _expiry_key(contract: dict[str, Any]) -> int:
    for key in ("delivery_date", "expiry", "due_date", "maturity_date"):
        parsed = _parse_date_key(contract.get(key))
        if parsed:
            return parsed
    code = str(contract.get("code") or "")
    match = re.search(r"(\d{6})", code)
    if match:
        return int(match.group(1)) * 100
    return 99999999


def _normalize_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = re.split(r"[|,]", raw)
    elif isinstance(raw, (list, tuple, set)):
        parts = list(raw)
    else:
        parts = [str(raw)]
    cleaned = []
    for item in parts:
        tag = str(item).strip()
        if tag:
            cleaned.append(tag)
    return cleaned


def _merge_tags(*tag_sets: Iterable[str]) -> list[str]:
    seen = set()
    merged = []
    for tags in tag_sets:
        for tag in tags:
            key = tag.lower()
            if key in seen:
                continue
            seen.add(key)
            merged.append(tag)
    return merged


def load_contract_cache(path: str = DEFAULT_CONTRACT_CACHE) -> ContractIndex | None:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            if path.endswith(".yaml") or path.endswith(".yml"):
                data = yaml.safe_load(f) or []
            else:
                data = json.load(f)
    except Exception as exc:
        logger.warning("Failed to load contract cache", path=path, error=str(exc))
        return None

    if isinstance(data, dict) and "contracts" in data:
        contracts = data.get("contracts", [])
    else:
        contracts = data if isinstance(data, list) else []

    return ContractIndex(contracts=contracts)


def write_contract_cache(contracts: list[dict[str, Any]], path: str = DEFAULT_CONTRACT_CACHE) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {"updated_at": datetime.utcnow().isoformat() + "Z", "contracts": contracts}
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, ensure_ascii=True)


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


def _parse_kv_tokens(tokens: list[str]) -> dict[str, Any]:
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
            attrs["tags"] = _normalize_tags(value)
        elif key in {"name", "contract_name"}:
            attrs["name"] = value
        elif key in {"contract_size", "size"}:
            try:
                attrs["contract_size"] = float(value)
            except ValueError:
                attrs.setdefault("_invalid", []).append(f"contract_size={value}")
    return attrs


def _parse_csv_spec(spec: str) -> tuple[str, dict[str, Any]]:
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
        attrs["tags"] = _normalize_tags(fields[4])
    return fields[0], attrs


def parse_symbols_list(
    path: str,
    contract_index: ContractIndex | None = None,
    result: SymbolBuildResult | None = None,
    seen: set[str] | None = None,
) -> SymbolBuildResult:
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
            spec = line

            if " " in line or "=" in line:
                tokens = line.split()
                spec_token = tokens[0]
                attrs = _parse_kv_tokens(tokens[1:])

                if "," in spec_token:
                    spec, csv_attrs = _parse_csv_spec(spec_token)
                    attrs = {**csv_attrs, **attrs}
                else:
                    spec = spec_token
            elif "," in line:
                spec, attrs = _parse_csv_spec(line)

            if attrs.get("_invalid"):
                for item in attrs.get("_invalid", []):
                    result.warnings.append(f"Invalid field in {path}: {line} ({item})")
                attrs.pop("_invalid", None)

            if not spec:
                result.warnings.append(f"Skipping empty spec in {path}: {line}")
                continue

            _expand_spec(spec, attrs, contract_index, result)

    return result


def _default_exchange_for_code(code: str) -> str:
    if code.isdigit():
        return "TSE"
    return "FUT"


def _expand_spec(
    spec: str,
    attrs: dict[str, Any],
    contract_index: ContractIndex | None,
    result: SymbolBuildResult,
) -> None:
    if "@" not in spec:
        entry = _build_entry(spec, attrs, None, result)
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
        month = parts[2] if len(parts) > 2 else "near"
        selector = parts[3] if len(parts) > 3 else "ATM"
        _expand_options(root, month, selector, attrs, contract_index, result)
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
        month = parts[2] if len(parts) > 2 else "front"
        _expand_futures(root, month, attrs, contract_index, result)
        return

    if len(parts) < 2:
        result.errors.append(f"Unknown rule spec: {spec}")
        return

    root = parts[0].upper()
    month = parts[1]
    _expand_futures(root, month, attrs, contract_index, result)


def _build_entry(
    code: str,
    attrs: dict[str, Any],
    contract: dict[str, Any] | None,
    result: SymbolBuildResult,
    extra_tags: list[str] | None = None,
) -> dict[str, Any] | None:
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

    tags = _merge_tags(entry.get("tags", []), extra_tags or [])
    if tags:
        entry["tags"] = tags

    return entry


def _group_by_expiry(contracts: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for contract in contracts:
        grouped.setdefault(_expiry_key(contract), []).append(contract)
    return [grouped[key] for key in sorted(grouped.keys())]


def _expand_synthetic(prefix: str, count: int, attrs: dict[str, Any], result: SymbolBuildResult) -> None:
    if count <= 0:
        result.errors.append(f"Synthetic count must be positive: {count}")
        return
    tags = ["synthetic", "stress"]
    for i in range(1, count + 1):
        code = f"{prefix.upper()}{i:04d}"
        entry = _build_entry(code, attrs, None, result, extra_tags=tags)
        if entry:
            entry.setdefault("exchange", "SIM")
            result.symbols.append(entry)


def _expand_futures(
    root: str,
    month_token: str,
    attrs: dict[str, Any],
    contract_index: ContractIndex | None,
    result: SymbolBuildResult,
) -> None:
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
        if not str(c.get("code", "")).endswith(("R1", "R2"))
        and not str(c.get("symbol", "")).endswith(("R1", "R2"))
    ]
    if not contracts:
        result.errors.append(f"No futures contracts found for root {root} after filtering R1/R2")
        return

    groups = _group_by_expiry(contracts)
    month = month_token.lower()
    idx_map = {"front": 0, "near": 0, "next": 1, "far": 2}
    idx = idx_map.get(month)
    if idx is None:
        result.errors.append(f"Unknown futures month selector: {month_token} ({root})")
        return
    if idx >= len(groups):
        result.errors.append(f"Futures month selector out of range: {root}@{month_token}")
        return

    selected = sorted(groups[idx], key=lambda c: str(c.get("code")))
    contract = selected[0]
    tags = ["futures", f"{month}_month", root.lower()]
    entry = _build_entry(str(contract.get("code")), attrs, contract, result, extra_tags=tags)
    if entry:
        entry.setdefault("exchange", "FUT")
        result.symbols.append(entry)


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


def _expand_options(
    root: str,
    month_token: str,
    selector: str,
    attrs: dict[str, Any],
    contract_index: ContractIndex | None,
    result: SymbolBuildResult,
) -> None:
    if not contract_index:
        result.errors.append(f"Option rule requires contract cache: OPT@{root}@{month_token}@{selector}")
        return

    contracts = contract_index.options_by_root.get(root)
    if not contracts:
        result.errors.append(f"No option contracts found for root {root}")
        return

    groups = _group_by_expiry(contracts)
    month = month_token.lower()
    idx_map = {"front": 0, "near": 0, "next": 1, "far": 2}
    idx = idx_map.get(month)
    if idx is None:
        result.errors.append(f"Unknown options month selector: {month_token} ({root})")
        return
    if idx >= len(groups):
        result.errors.append(f"Options month selector out of range: {root}@{month_token}")
        return

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

    right_needed = {"C", "P"}
    mode_tag = "atm" if mode == "ATM" else "otm"
    tags = ["options", f"{month}_month", mode_tag, root.lower()]

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
        if right not in right_needed:
            continue
        entry = _build_entry(str(contract.get("code")), attrs, contract, result, extra_tags=tags)
        if entry:
            entry.setdefault("exchange", "OPT")
            result.symbols.append(entry)


def build_symbols(
    list_path: str = DEFAULT_LIST_PATH,
    contract_index: ContractIndex | None = None,
) -> SymbolBuildResult:
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


def write_symbols_yaml(symbols: list[dict[str, Any]], output_path: str = DEFAULT_OUTPUT_PATH) -> None:
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w") as f:
        yaml.safe_dump({"symbols": symbols}, f, sort_keys=False)


def validate_symbols(
    symbols: list[dict[str, Any]],
    contract_index: ContractIndex | None = None,
    max_subscriptions: int = 200,
) -> SymbolBuildResult:
    result = SymbolBuildResult(symbols=symbols)
    seen: set[str] = set()

    for entry in symbols:
        code = str(entry.get("code") or "")
        if not code:
            result.errors.append("Symbol entry missing code")
            continue
        if code in seen:
            result.errors.append(f"Duplicate symbol code: {code}")
        seen.add(code)

        exchange = str(entry.get("exchange") or "").upper()
        if not exchange:
            result.errors.append(f"Missing exchange for {code}")
        elif exchange not in VALID_EXCHANGES:
            result.errors.append(f"Unknown exchange for {code}: {exchange}")

        tick_size = entry.get("tick_size")
        if tick_size is not None:
            try:
                if float(tick_size) <= 0:
                    result.errors.append(f"Invalid tick_size for {code}: {tick_size}")
            except (TypeError, ValueError):
                result.errors.append(f"Invalid tick_size for {code}: {tick_size}")

        price_scale = entry.get("price_scale")
        if price_scale is not None:
            try:
                if int(price_scale) <= 0:
                    result.errors.append(f"Invalid price_scale for {code}: {price_scale}")
            except (TypeError, ValueError):
                result.errors.append(f"Invalid price_scale for {code}: {price_scale}")

    if len(symbols) > max_subscriptions:
        result.errors.append(f"Symbol count exceeds subscription limit: {len(symbols)} > {max_subscriptions}")

    if contract_index:
        for entry in symbols:
            code = str(entry.get("code") or "")
            exchange = str(entry.get("exchange") or "").upper()
            if exchange == "SIM":
                continue
            if code and code not in contract_index.by_code:
                result.errors.append(f"Unsubscribable symbol (not in contract cache): {code}")

    return result


def preview_lines(result: SymbolBuildResult, sample: int = 10) -> list[str]:
    lines = []
    lines.append(f"symbols={len(result.symbols)}")
    if result.symbols:
        sample_items = result.symbols[:sample]
        rendered = ", ".join(
            f"{item.get('code')}({item.get('exchange', '')})" for item in sample_items if item.get("code")
        )
        lines.append(f"sample={rendered}")
    if result.errors or result.warnings:
        lines.append(f"errors={len(result.errors)} warnings={len(result.warnings)}")
    return lines


def fetch_contracts_from_broker() -> list[dict[str, Any]]:
    try:
        import shioaji as sj
    except Exception as exc:  # pragma: no cover - environment missing SDK
        raise RuntimeError("shioaji SDK not available") from exc

    pid = os.getenv("SHIOAJI_PERSON_ID") or os.getenv("SHIOAJI_API_KEY")
    pwd = os.getenv("SHIOAJI_PASSWORD") or os.getenv("SHIOAJI_SECRET_KEY")
    if not pid or not pwd:
        raise RuntimeError("SHIOAJI credentials missing (env vars)")

    api = sj.Shioaji(simulation=True)
    if len(pid) > 20:
        api.login(api_key=pid, secret_key=pwd, contracts_timeout=60000)
    else:
        api.login(person_id=pid, passwd=pwd, contracts_timeout=60000)

    contracts: list[dict[str, Any]] = []

    def normalize(contract: Any, exchange: str, kind: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": getattr(contract, "code", None),
            "symbol": getattr(contract, "symbol", None),
            "name": getattr(contract, "name", None),
            "exchange": exchange,
            "type": kind,
            "root": getattr(contract, "category", None) or getattr(contract, "symbol", None),
            "tick_size": getattr(contract, "tick_size", None),
            "price_scale": getattr(contract, "price_scale", None),
            "contract_size": getattr(contract, "contract_size", None),
            "delivery_date": getattr(contract, "delivery_date", None),
            "strike": getattr(contract, "strike_price", None) or getattr(contract, "strike", None),
            "right": getattr(contract, "option_right", None) or getattr(contract, "right", None),
            "reference": getattr(contract, "reference", None),
        }
        return {k: v for k, v in payload.items() if v is not None}

    try:
        for contract in api.Contracts.Stocks.TSE:
            contracts.append(normalize(contract, "TSE", "stock"))
    except Exception as exc:
        logger.warning("Failed to fetch TSE contracts", error=str(exc))

    try:
        for contract in api.Contracts.Stocks.OTC:
            contracts.append(normalize(contract, "OTC", "stock"))
    except Exception as exc:
        logger.warning("Failed to fetch OTC contracts", error=str(exc))

    try:
        for contract in api.Contracts.Futures:
            contracts.append(normalize(contract, "FUT", "future"))
    except Exception as exc:
        logger.warning("Failed to fetch Futures contracts", error=str(exc))

    try:
        for contract in api.Contracts.Options:
            contracts.append(normalize(contract, "OPT", "option"))
    except Exception as exc:
        logger.warning("Failed to fetch Options contracts", error=str(exc))

    try:
        for contract in api.Contracts.Indexs.TSE:
            contracts.append(normalize(contract, "IDX", "index"))
    except Exception as exc:
        logger.warning("Failed to fetch TSE Indexs contracts", error=str(exc))

    try:
        for contract in api.Contracts.Indexs.OTC:
            contracts.append(normalize(contract, "IDX", "index"))
    except Exception as exc:
        logger.warning("Failed to fetch OTC Indexs contracts", error=str(exc))

    return contracts
