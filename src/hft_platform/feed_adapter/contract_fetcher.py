"""Centralised broker contract fetching — keeps shioaji imports inside feed_adapter/.

All Shioaji SDK usage for contract resolution and fetching lives here.
Platform modules (cli, config) delegate to these functions.
"""

from __future__ import annotations

import os
from typing import Any

from structlog import get_logger

logger = get_logger("feed_adapter.contract_fetcher")

try:
    import shioaji as sj
except ImportError:
    sj = None


def _login_shioaji() -> Any:
    """Login to Shioaji in simulation mode and return the API instance."""
    if sj is None:
        raise RuntimeError("shioaji SDK not installed")
    api_key = os.environ.get("SHIOAJI_API_KEY")
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError("SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY env vars required")
    api = sj.Shioaji(simulation=True)
    api.login(api_key=api_key, secret_key=secret_key, contracts_timeout=60000)
    return api


def resolve_symbol_exchanges(symbols: list[str]) -> list[dict[str, str]]:
    """Resolve TSE/OTC exchange for a list of symbol codes via Shioaji."""
    api = _login_shioaji()
    code_map: dict[str, str] = {}
    try:
        for c in api.Contracts.Stocks.TSE:
            code_map[c.code] = "TSE"
        for c in api.Contracts.Stocks.OTC:
            code_map[c.code] = "OTC"
    except Exception as exc:
        logger.warning("contract_fetch_warning", error=str(exc))
    result: list[dict[str, str]] = []
    for code in symbols:
        exch = code_map.get(code)
        if exch:
            result.append({"code": code, "exchange": exch})
        else:
            logger.warning("symbol_not_found", code=code)
    return result


def _normalize_contract(contract: Any, exchange: str, kind: str) -> dict[str, Any]:
    """Normalize a single broker contract to a plain dict."""
    right = getattr(contract, "option_right", None) or getattr(contract, "right", None)
    if right is not None:
        right = getattr(right, "value", right)
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
        "right": right,
        "reference": getattr(contract, "reference", None),
    }
    return {k: v for k, v in payload.items() if v is not None}


def fetch_all_contracts() -> list[dict[str, Any]]:
    """Fetch all available contracts from Shioaji broker SDK."""
    api = _login_shioaji()
    if hasattr(api, "fetch_contracts"):
        try:
            api.fetch_contracts(contract_download=True)
        except Exception as exc:
            logger.warning("contract_refresh_failed", error=str(exc))
    contracts: list[dict[str, Any]] = []
    for label, iterable, kind in [
        ("TSE stocks", lambda: api.Contracts.Stocks.TSE, "stock"),
        ("OTC stocks", lambda: api.Contracts.Stocks.OTC, "stock"),
        ("TSE indices", lambda: api.Contracts.Indexs.TSE, "index"),
        ("OTC indices", lambda: api.Contracts.Indexs.OTC, "index"),
    ]:
        exchange = label.split()[0].upper()
        if kind == "index":
            exchange = "IDX"
        try:
            for c in iterable():
                contracts.append(_normalize_contract(c, exchange, kind))
        except Exception as exc:
            logger.warning("contract_fetch_failed", label=label, error=str(exc))
    try:
        for root in api.Contracts.Futures.keys():
            try:
                for c in api.Contracts.Futures[root]:
                    contracts.append(_normalize_contract(c, "FUT", "future"))
            except Exception as exc:
                logger.warning("futures_root_fetch_failed", root=root, error=str(exc))
    except Exception as exc:
        logger.warning("futures_keys_failed", error=str(exc))
    try:
        for root in api.Contracts.Options.keys():
            try:
                for c in api.Contracts.Options[root]:
                    contracts.append(_normalize_contract(c, "OPT", "option"))
            except Exception as exc:
                logger.warning("options_root_fetch_failed", root=root, error=str(exc))
    except Exception as exc:
        logger.warning("options_keys_failed", error=str(exc))
    return contracts
