"""Centralised broker contract fetching."""

from __future__ import annotations

import os

from structlog import get_logger

logger = get_logger("feed_adapter.contract_fetcher")
try:
    import shioaji as sj
except ImportError:
    sj = None


def _login_shioaji():
    if sj is None:
        raise RuntimeError("shioaji SDK not installed")
    ak, sk = os.environ.get("SHIOAJI_API_KEY"), os.environ.get("SHIOAJI_SECRET_KEY")
    if not ak or not sk:
        raise RuntimeError("SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY required")
    api = sj.Shioaji(simulation=True)
    api.login(api_key=ak, secret_key=sk, contracts_timeout=60000)
    return api


def resolve_symbol_exchanges(symbols):
    api = _login_shioaji()
    code_map = {}
    try:
        for c in api.Contracts.Stocks.TSE:
            code_map[c.code] = "TSE"
        for c in api.Contracts.Stocks.OTC:
            code_map[c.code] = "OTC"
    except Exception as exc:
        logger.warning("contract_fetch_warning", error=str(exc))
    result = []
    for code in symbols:
        exch = code_map.get(code)
        if exch:
            result.append({"code": code, "exchange": exch})
        else:
            logger.warning("symbol_not_found", code=code)
    return result


def _norm(contract, exchange, kind):
    right = getattr(contract, "option_right", None) or getattr(contract, "right", None)
    if right is not None:
        right = getattr(right, "value", right)
    p = {
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
    return {k: v for k, v in p.items() if v is not None}


def fetch_all_contracts():
    api = _login_shioaji()
    if hasattr(api, "fetch_contracts"):
        try:
            api.fetch_contracts(contract_download=True)
        except Exception as exc:
            logger.warning("contract_refresh_failed", error=str(exc))
    contracts = []
    for label, it, kind in [
        ("TSE", lambda: api.Contracts.Stocks.TSE, "stock"),
        ("OTC", lambda: api.Contracts.Stocks.OTC, "stock"),
        ("IDX", lambda: api.Contracts.Indexs.TSE, "index"),
        ("IDX", lambda: api.Contracts.Indexs.OTC, "index"),
    ]:
        try:
            for c in it():
                contracts.append(_norm(c, label, kind))
        except Exception as exc:
            logger.warning("fetch_failed", label=label, error=str(exc))
    for group_name, group_attr, exch, kind in [
        ("Futures", "Futures", "FUT", "future"),
        ("Options", "Options", "OPT", "option"),
    ]:
        try:
            grp = getattr(api.Contracts, group_attr)
            for root in grp.keys():
                try:
                    for c in grp[root]:
                        contracts.append(_norm(c, exch, kind))
                except Exception as exc:
                    logger.warning(f"{group_name.lower()}_root_failed", root=root, error=str(exc))
        except Exception as exc:
            logger.warning(f"{group_name.lower()}_keys_failed", error=str(exc))
    return contracts
