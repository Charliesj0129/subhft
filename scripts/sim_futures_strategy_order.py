#!/usr/bin/env python3
import argparse
import asyncio
import os
import sys
import time
from pathlib import Path


def load_env():
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _resolve_contract(symbol: str, value):
    if value is None:
        return "", None
    if hasattr(value, "reference"):
        return symbol, value
    try:
        for item in value:
            if hasattr(item, "reference"):
                return getattr(item, "code", symbol), item
    except Exception:
        return "", None
    return "", None


def _pick_contract(api, preferred: str):
    if preferred:
        try:
            symbol, contract = _resolve_contract(preferred, api.Contracts.Futures[preferred])
            if contract:
                return symbol, contract
        except Exception:
            pass
    for symbol in ("TXFC0", "MXFC0", "TXF", "MXF"):
        try:
            sym, contract = _resolve_contract(symbol, api.Contracts.Futures[symbol])
            if contract:
                return sym, contract
        except Exception:
            continue
    try:
        if hasattr(api.Contracts.Futures, "keys"):
            for symbol in api.Contracts.Futures.keys():
                try:
                    sym, contract = _resolve_contract(symbol, api.Contracts.Futures[symbol])
                    if contract:
                        return sym, contract
                except Exception:
                    continue
    except Exception:
        pass
    try:
        for item in api.Contracts.Futures:
            sym, contract = _resolve_contract(getattr(item, "code", ""), item)
            if contract:
                return sym, contract
    except Exception:
        pass
    return "", None


def _trade_status(trade):
    if trade is None:
        return {}, {}
    status = getattr(trade, "status", None)
    op = getattr(trade, "operation", None)
    status_info = {
        "status_code": getattr(status, "status_code", ""),
        "status_state": str(getattr(status, "status", "")),
        "status_msg": getattr(status, "msg", ""),
    }
    op_info = {
        "op_code": op.get("op_code") if isinstance(op, dict) else getattr(op, "op_code", ""),
        "op_msg": op.get("op_msg") if isinstance(op, dict) else getattr(op, "op_msg", ""),
    }
    return status_info, op_info


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=os.getenv("HFT_FUTURES_SYMBOL", "TXFC0"))
    args = parser.parse_args()

    load_env()
    os.environ["HFT_MODE"] = "sim"
    os.environ.setdefault("SHIOAJI_FETCH_CONTRACT", "1")
    os.environ.setdefault("SHIOAJI_ACTIVATE_CA", "0")
    os.environ.setdefault("HFT_ACTIVATE_CA", "0")

    from hft_platform.contracts.strategy import IntentType, OrderCommand, OrderIntent, Side, StormGuardState, TIF
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient
    from hft_platform.order.adapter import OrderAdapter

    api_key = os.getenv("SHIOAJI_API_KEY") or os.getenv("SHIOAJI_APIKEY")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY") or os.getenv("SHIOAJI_SECRETKEY")
    if not api_key or not secret_key:
        print("Missing SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY in env.")
        return 1

    import shioaji as sj

    api = sj.Shioaji(simulation=True)
    api.login(api_key=api_key, secret_key=secret_key)
    api.fetch_contracts(contract_download=True)

    client = ShioajiClient()
    client.api = api
    client.mode = "simulation"

    symbol = ""
    contract = None
    for _ in range(10):
        symbol, contract = _pick_contract(api, args.symbol)
        if contract:
            break
        time.sleep(0.5)
    if not contract:
        symbols = []
        try:
            if hasattr(api.Contracts.Futures, "keys"):
                symbols = list(api.Contracts.Futures.keys())
            else:
                symbols = [c.code for c in api.Contracts.Futures if hasattr(c, "code")]
        except Exception:
            symbols = []
        print("No futures contract found in simulation.")
        print(f"available_futures={symbols[:20]}")
        return 3

    exchange_raw = getattr(contract, "exchange", "") or "TAIFEX"
    exchange = str(exchange_raw)
    if "TAIFEX" in exchange:
        exchange = "TAIFEX"

    order_queue: asyncio.Queue = asyncio.Queue()
    adapter = OrderAdapter("config/order_adapter.yaml", order_queue, client)

    if symbol not in adapter.metadata.meta:
        adapter.metadata.meta[symbol] = {"code": symbol, "exchange": exchange, "product_type": "future"}
        adapter.metadata._exchange_cache.pop(symbol, None)
        adapter.metadata._product_type_cache.pop(symbol, None)

    price_base = getattr(contract, "reference", None) or getattr(contract, "limit_down", None) or getattr(
        contract, "limit_up", None
    )
    if not price_base:
        print("No price available from contract reference/limits.")
        return 4

    price_int = adapter.price_codec.scale(symbol, price_base)
    intent = OrderIntent(
        intent_id=1,
        strategy_id="simstr",
        symbol=symbol,
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=price_int,
        qty=1,
        tif=TIF.LIMIT,
        timestamp_ns=time.time_ns(),
    )
    cmd = OrderCommand(
        cmd_id=1,
        intent=intent,
        deadline_ns=time.time_ns() + 5_000_000_000,
        storm_guard_state=StormGuardState.NORMAL,
    )

    await adapter.execute(cmd)

    trade = adapter.live_orders.get("simstr:1")
    status_info, op_info = _trade_status(trade)

    try:
        api.update_status(api.futopt_account)
    except Exception:
        pass

    trades = []
    try:
        trades = api.list_trades()
    except Exception:
        trades = []

    print(f"order_symbol={symbol}")
    print(f"order_exchange={exchange}")
    print(f"order_price={price_base}")
    print(f"order_price_int={price_int}")
    print(f"status_code={status_info.get('status_code','')}")
    print(f"status_state={status_info.get('status_state','')}")
    print(f"status_msg={status_info.get('status_msg','')}")
    print(f"op_code={op_info.get('op_code','')}")
    print(f"op_msg={op_info.get('op_msg','')}")
    print(f"trades_count={len(trades)}")

    api.logout()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
