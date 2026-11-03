"""Contract cache I/O and broker fetch for the symbols subsystem.

Handles loading/writing contract caches (JSON/YAML), metrics caches,
and fetching live contracts from the Shioaji broker SDK.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml
from structlog import get_logger

from hft_platform.config._symbols_types import (
    DEFAULT_CONTRACT_CACHE,
    DEFAULT_METRICS_CACHE,
    DEFAULT_METRICS_ENV,
    DEFAULT_OUTPUT_PATH,
    ContractIndex,
    SymbolBuildResult,
)

logger = get_logger("config.symbols.contracts")


# ---------------------------------------------------------------------------
# Metrics cache
# ---------------------------------------------------------------------------


def load_metrics_cache(path: str = DEFAULT_METRICS_CACHE) -> dict[str, dict[str, Any]]:
    """Load a metrics cache file (JSON or YAML) into a code-keyed dict."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            if path.endswith(".yaml") or path.endswith(".yml"):
                data = yaml.safe_load(f) or {}
            else:
                data = json.load(f)
    except Exception as exc:
        logger.warning("Failed to load metrics cache", path=path, error=str(exc))
        return {}

    if isinstance(data, dict) and "metrics" in data:
        data = data.get("metrics", {})

    if isinstance(data, dict):
        metrics: dict[str, dict[str, Any]] = {}
        for code, payload in data.items():
            key = str(code or "").strip()
            if not key:
                continue
            metrics[key] = payload if isinstance(payload, dict) else {"value": payload}
        return metrics

    if isinstance(data, list):
        metrics = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            code = item.get("code") or item.get("symbol")
            if not code:
                continue
            key = str(code).strip()
            payload_dict = {k: v for k, v in item.items() if k not in {"code", "symbol"}}
            metrics[key] = payload_dict
        return metrics

    return {}


# ---------------------------------------------------------------------------
# Contract cache I/O
# ---------------------------------------------------------------------------


def load_contract_cache(path: str = DEFAULT_CONTRACT_CACHE, metrics_path: str | None = None) -> ContractIndex | None:
    """Load a contract cache file and return a ``ContractIndex``."""
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

    resolved_metrics_path = metrics_path
    if resolved_metrics_path is None:
        resolved_metrics_path = os.getenv(DEFAULT_METRICS_ENV)
    if resolved_metrics_path is None and os.path.exists(DEFAULT_METRICS_CACHE):
        resolved_metrics_path = DEFAULT_METRICS_CACHE

    metrics = load_metrics_cache(resolved_metrics_path) if resolved_metrics_path else {}

    return ContractIndex(contracts=contracts, metrics_by_code=metrics)


def _json_cache_default(value: Any) -> Any:
    """Serialise the SDK dates JSON rejects, in the cache's ``YYYY/MM/DD`` form.

    shioaji 1.7.9 returns ``datetime.date`` for ``delivery_date`` and
    ``update_date`` where 1.5.6 returned ``"2026/11/18"``. Any other non-JSON
    type is still an error: a silently stringified unknown type would put a
    value no reader parses into the cache.
    """
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.strftime("%Y/%m/%d")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


#: A rewrite that keeps less than this share of the previous cache's
#: futures/options rows is a partial download, not a listing change. A monthly
#: settlement moves a handful of rows; it never halves a category.
CONTRACT_CACHE_MIN_KEEP_RATIO = 0.5

_DERIVATIVE_KINDS = ("future", "option")


class ContractCacheRegressionError(ValueError):
    """A contract rewrite would drop roots or rows the current cache holds.

    Raised by :func:`write_contract_cache` before anything is written: the
    cache on disk stays the last complete download.
    """

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _explicit_derivative_roots(contracts: list[dict[str, Any]]) -> dict[str, set[str]]:
    """Return ``{kind: {root}}`` for futures/options rows that carry a root.

    Only an explicit ``root`` (or the 1.5.6 ``category``) counts. Deriving one
    from the code would read ``TXFK6`` as ``TXFK``, a different "root" every
    month, and turn each settlement into a false regression.
    """
    roots: dict[str, set[str]] = {kind: set() for kind in _DERIVATIVE_KINDS}
    for row in contracts:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("type") or row.get("security_type") or "").strip().lower()
        if kind not in roots:
            continue
        root = str(row.get("root") or row.get("category") or "").strip().upper()
        if root:
            roots[kind].add(root)
    return roots


def contract_cache_regressions(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[str]:
    """Return why replacing *before* with *after* would lose contracts; empty means safe.

    Two signals of a partial broker download (2026-11-02: a rewrite lost every
    TXF and MXF row, and the boot universe roll could not resolve them):

    - a futures/options root present before is absent after;
    - a futures/options category keeps less than
      :data:`CONTRACT_CACHE_MIN_KEEP_RATIO` of its rows.

    A root the exchange really delists trips the first check too. That is the
    intended direction: the old cache stays until an operator writes the new
    one with ``allow_regression=True``.
    """
    reasons: list[str] = []
    roots_before = _explicit_derivative_roots(before)
    roots_after = _explicit_derivative_roots(after)
    for kind in _DERIVATIVE_KINDS:
        missing = sorted(roots_before[kind] - roots_after[kind])
        if missing:
            reasons.append(f"{kind} roots missing: {', '.join(missing[:10])}")

    def _count(rows: list[dict[str, Any]], kind: str) -> int:
        return sum(
            1
            for row in rows
            if isinstance(row, dict) and str(row.get("type") or row.get("security_type") or "").strip().lower() == kind
        )

    for kind in _DERIVATIVE_KINDS:
        n_before = _count(before, kind)
        n_after = _count(after, kind)
        if n_before > 0 and n_after < n_before * CONTRACT_CACHE_MIN_KEEP_RATIO:
            reasons.append(f"{kind} rows {n_before} -> {n_after}")
    return reasons


def write_contract_cache(
    contracts: list[dict[str, Any]],
    path: str = DEFAULT_CONTRACT_CACHE,
    *,
    allow_regression: bool = False,
) -> None:
    """Atomically write *contracts* to a versioned JSON cache file.

    Refuses (raises :class:`ContractCacheRegressionError`, writes nothing) when
    the rewrite would drop futures/options the current cache holds -- see
    :func:`contract_cache_regressions`. ``allow_regression=True`` is for an
    operator who has checked the listing really shrank.
    """
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Read existing cache_version and increment
    cache_version = 0
    existing_contracts: list[dict[str, Any]] = []
    if dest.exists():
        try:
            existing = json.loads(dest.read_text(encoding="utf-8"))
            cache_version = int(existing.get("cache_version", 0))
            rows = existing.get("contracts", [])
            existing_contracts = rows if isinstance(rows, list) else []
        except Exception as exc:
            # An unreadable cache has nothing to protect: overwrite it.
            logger.debug("operation_fallback", error=str(exc))
    if not allow_regression:
        reasons = contract_cache_regressions(existing_contracts, contracts)
        if reasons:
            logger.error(
                "contract_cache_write_refused",
                path=str(dest),
                reasons=reasons,
                existing_count=len(existing_contracts),
                new_count=len(contracts),
            )
            raise ContractCacheRegressionError(reasons)
    cache_version += 1

    payload = {
        "cache_version": cache_version,
        "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "contracts": contracts,
    }
    tmp = dest.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=True, default=_json_cache_default)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        # json.dump streams, so a failure part-way leaves a truncated temp file
        # (THESHOW kept a 390 KB contracts.json.tmp). The live cache is
        # untouched either way; do not leave the fragment beside it.
        tmp.unlink(missing_ok=True)
        raise
    tmp.rename(dest)
    logger.info("contract_cache_written", cache_version=cache_version, contract_count=len(contracts))


def write_symbols_yaml(symbols: list[dict[str, Any]], output_path: str = DEFAULT_OUTPUT_PATH) -> None:
    """Atomically write *symbols* to a YAML config file."""
    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump({"symbols": symbols}, f, sort_keys=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.rename(dest)


# ---------------------------------------------------------------------------
# Broker fetch
# ---------------------------------------------------------------------------


def fetch_contracts_from_broker() -> list[dict[str, Any]]:
    """Fetch all available contracts from the broker SDK.

    Delegates to feed_adapter.contract_fetcher (MB-02 compliance).
    """
    from hft_platform.feed_adapter.contract_fetcher import fetch_all_contracts

    return fetch_all_contracts()


# ---------------------------------------------------------------------------
# Validation & preview helpers
# ---------------------------------------------------------------------------


def validate_symbols(
    symbols: list[dict[str, Any]],
    contract_index: ContractIndex | None = None,
    max_subscriptions: int = 480,
) -> SymbolBuildResult:
    """Validate a list of symbol entries for correctness and subscription limits.

    Default cap is the platform-wide total (4 quote connections × 120 codes per
    conn). The per-conn cap is in *codes* — each code subscribes to 2 broker
    topics (Tick + BidAsk), and the Solace per-session topic budget for SinoPac
    retail accounts is ~250 (so 120 codes × 2 topics = 240, leaves headroom).
    See ``feed_adapter/shioaji/limits.py``.
    """
    from hft_platform.config._symbols_types import VALID_EXCHANGES

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


def preview_lines(
    result: SymbolBuildResult,
    sample: int = 10,
    validation: SymbolBuildResult | None = None,
) -> list[str]:
    """Generate human-readable preview lines for a build result.

    ``validation`` is the separate :func:`validate_symbols` result. Pass it
    whenever the caller also runs validation: the summary line is the first
    thing an operator reads, and reporting ``errors=0`` above an ``Errors:``
    section listing a fatal validation failure reads as a clean build. That
    matters most during a contract roll, when the rebuild is run against a
    deadline and the subscription-limit error is the one that aborts it.
    """
    errors = list(result.errors)
    warnings = list(result.warnings)
    if validation is not None:
        errors += validation.errors
        warnings += validation.warnings

    lines: list[str] = []
    lines.append(f"symbols={len(result.symbols)}")
    if result.symbols:
        sample_items = result.symbols[:sample]
        rendered = ", ".join(
            f"{item.get('code')}({item.get('exchange', '')})" for item in sample_items if item.get("code")
        )
        lines.append(f"sample={rendered}")
    if errors or warnings:
        lines.append(f"errors={len(errors)} warnings={len(warnings)}")
    return lines
