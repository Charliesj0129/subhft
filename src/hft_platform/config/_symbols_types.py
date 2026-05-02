"""Shared types and constants for the symbols subsystem.

Contains dataclasses (``SymbolBuildResult``, ``ContractIndex``, ``FilterSpec``),
constants (valid exchanges, filter keys, metric aliases), and tiny pure helpers
that multiple sibling modules depend on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_LIST_PATH = "config/symbols.list"
DEFAULT_OUTPUT_PATH = "config/symbols.yaml"
DEFAULT_CONTRACT_CACHE = "config/contracts.json"
DEFAULT_METRICS_CACHE = "config/metrics.json"
DEFAULT_METRICS_ENV = "HFT_SYMBOL_METRICS"
PLUS_MINUS = "\u00b1"

VALID_EXCHANGES = {"TSE", "OTC", "OES", "FUT", "FUTURES", "OPT", "OPTIONS", "TAIFEX", "IDX", "INDEX", "SIM"}

FILTER_KEYS = {
    "tradable",
    "margin",
    "oi",
    "trades_per_min",
    "price",
    "premium",
    "delta",
    "iv_rank",
    "exclude_dte",
    "dte",
    "chg_pct",
    "intraday_range",
    "avg_vol_20d",
    "turnover_rank",
    "sector",
    "weight",
    "month",
    "moneyness",
    "hedge_with",
    "exclude",
}
FILTER_BOOL_KEYS = {"tradable"}
FILTER_LIST_KEYS = {"margin", "sector", "month", "exclude", "hedge_with"}

METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "oi": ("oi", "open_interest"),
    "trades_per_min": ("trades_per_min", "trades_per_minute", "tpm"),
    "price": ("price", "last_price", "last", "close", "reference"),
    "premium": ("premium", "option_premium", "last_price", "close"),
    "delta": ("delta",),
    "iv_rank": ("iv_rank", "iv_percentile"),
    "chg_pct": ("chg_pct", "change_pct", "pct_change"),
    "intraday_range": ("intraday_range", "range_pct", "range"),
    "avg_vol_20d": ("avg_vol_20d", "avg_volume_20d", "avg20d_vol"),
    "turnover_rank": ("turnover_rank",),
    "turnover": ("turnover", "turnover_value", "turnover_amt"),
    "margin": ("margin", "margin_level"),
    "sector": ("sector", "industry"),
    "weight": ("weight", "index_weight"),
    "hedge_with": ("hedge_with",),
    "tradable": ("tradable", "is_tradable"),
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SymbolBuildResult:
    symbols: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.errors


@dataclass
class FilterSpec:
    bools: dict[str, bool] = field(default_factory=dict)
    enums: dict[str, set[str]] = field(default_factory=dict)
    numeric_min: dict[str, float] = field(default_factory=dict)
    numeric_max: dict[str, float] = field(default_factory=dict)
    top_n: dict[str, int] = field(default_factory=dict)
    exclude_flags: set[str] = field(default_factory=set)
    months: list[str] | None = None
    roll: bool = False
    roll_dte_max: int | None = None
    exclude_dte_max: int | None = None

    def merge_months(self, months: list[str]) -> None:
        if not months:
            return
        if self.months is None:
            self.months = months
            return
        seen = set(self.months)
        for month in months:
            if month not in seen:
                self.months.append(month)
                seen.add(month)


# ---------------------------------------------------------------------------
# Tiny pure helpers used by multiple sibling modules
# ---------------------------------------------------------------------------


def derive_root(code: str) -> str:
    match = re.match(r"([A-Za-z]+)", code)
    return match.group(1) if match else code


def parse_date_key(value: Any) -> int | None:
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


def expiry_key(contract: dict[str, Any]) -> int:
    for key in ("delivery_date", "expiry", "due_date", "maturity_date"):
        parsed = parse_date_key(contract.get(key))
        if parsed:
            return parsed
    code = str(contract.get("code") or "")
    match = re.search(r"(\d{6})", code)
    if match:
        return int(match.group(1)) * 100
    return 99999999


def contract_dte_days(contract: dict[str, Any]) -> int | None:
    for key in ("delivery_date", "expiry", "due_date", "maturity_date"):
        parsed = parse_date_key(contract.get(key))
        if parsed:
            try:
                date_val = datetime.strptime(str(parsed), "%Y%m%d").date()
                return (date_val - datetime.now(UTC).date()).days
            except ValueError:
                continue
    return None


@dataclass
class ContractIndex:
    contracts: list[dict[str, Any]]
    metrics_by_code: dict[str, dict[str, Any]] = field(default_factory=dict)
    by_code: dict[str, dict[str, Any]] = field(default_factory=dict)
    futures_by_root: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    options_by_root: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        normalized_metrics: dict[str, dict[str, Any]] = {}
        if self.metrics_by_code:
            for raw_code, payload in self.metrics_by_code.items():
                code = str(raw_code or "").strip()
                if not code:
                    continue
                if isinstance(payload, dict):
                    normalized_metrics[code] = payload
                else:
                    normalized_metrics[code] = {"value": payload}
        self.metrics_by_code = normalized_metrics
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
