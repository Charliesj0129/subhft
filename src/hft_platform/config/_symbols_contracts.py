"""Contract cache I/O and broker fetch for the symbols subsystem.

Handles loading/writing contract caches (JSON/YAML), metrics caches,
and fetching live contracts from the Shioaji broker SDK.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
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


def write_contract_cache(contracts: list[dict[str, Any]], path: str = DEFAULT_CONTRACT_CACHE) -> None:
    """Atomically write *contracts* to a versioned JSON cache file."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Read existing cache_version and increment
    cache_version = 0
    if dest.exists():
        try:
            existing = json.loads(dest.read_text(encoding="utf-8"))
            cache_version = int(existing.get("cache_version", 0))
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            pass
    cache_version += 1

    payload = {
        "cache_version": cache_version,
        "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "contracts": contracts,
    }
    tmp = dest.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=True)
        f.flush()
        os.fsync(f.fileno())
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


def preview_lines(result: SymbolBuildResult, sample: int = 10) -> list[str]:
    """Generate human-readable preview lines for a build result."""
    lines: list[str] = []
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
