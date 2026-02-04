#!/usr/bin/env python3
import argparse
import os
import sys
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--contracts-path", dest="contracts_path", default="")
    parser.add_argument("--symbol", dest="symbol", default="")
    args = parser.parse_args()

    load_env()
    if args.contracts_path:
        os.environ["SJ_CONTRACTS_PATH"] = args.contracts_path

    import shioaji as sj
    api_key = os.getenv("SHIOAJI_API_KEY") or os.getenv("SHIOAJI_APIKEY")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY") or os.getenv("SHIOAJI_SECRETKEY")

    if not api_key or not secret_key:
        print("Missing SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY in env.")
        sys.exit(1)

    api = sj.Shioaji(simulation=True)
    accounts = api.login(api_key=api_key, secret_key=secret_key)
    api.fetch_contracts(contract_download=True)

    print(f"accounts_count={len(accounts)}")
    for acc in accounts:
        print(
            f"account_type={acc.account_type} id={acc.account_id} broker={acc.broker_id} signed={acc.signed}"
        )

    stock_account = api.stock_account
    if stock_account is None:
        print("stock_account=None")
        api.logout()
        sys.exit(2)

    print(f"stock_account_id={stock_account.account_id} signed={stock_account.signed}")

    stock_count = 0
    try:
        stock_count = len(api.Contracts.Stocks)
    except Exception:
        try:
            stock_count = len([c for c in api.Contracts.Stocks])
        except Exception:
            stock_count = 0
    print(f"stocks_total={stock_count}")

    symbol = args.symbol or os.getenv("SHIOAJI_TEST_SYMBOL", "2330")
    contract = api.Contracts.Stocks.get(symbol) if hasattr(api.Contracts.Stocks, "get") else None
    if contract is None:
        try:
            contract = api.Contracts.Stocks[symbol]
        except Exception:
            contract = None

    print(f"contract_found={contract is not None}")
    if contract is not None:
        print(f"contract_code={contract.code} exchange={contract.exchange} ref={contract.reference}")

    try:
        snaps = api.snapshots([contract]) if contract is not None else []
        print(f"snapshots_count={len(snaps)}")
    except Exception as exc:
        print(f"snapshots_error={exc}")

    try:
        api.update_status(stock_account)
        print("update_status=ok")
    except Exception as exc:
        print(f"update_status_error={exc}")

    if contract is None:
        api.logout()
        sys.exit(3)

    order = api.Order(
        price=contract.reference,
        quantity=1,
        action=sj.constant.Action.Buy,
        price_type=sj.constant.StockPriceType.LMT,
        order_type=sj.constant.OrderType.ROD,
        order_cond=sj.constant.StockOrderCond.Cash,
        order_lot=sj.constant.StockOrderLot.Common,
        account=stock_account,
    )

    trade = api.place_order(contract, order)
    op = getattr(trade, "operation", None)
    op_code = op.get("op_code") if isinstance(op, dict) else getattr(op, "op_code", "")
    op_msg = op.get("op_msg") if isinstance(op, dict) else getattr(op, "op_msg", "")
    status_code = getattr(trade.status, "status_code", "")
    status_msg = getattr(trade.status, "msg", "")
    status_state = str(getattr(trade.status, "status", ""))

    print(f"order_op_code={op_code}")
    print(f"order_op_msg={op_msg}")
    print(f"order_status_code={status_code}")
    print(f"order_status_msg={status_msg}")
    print(f"order_status_state={status_state}")

    api.logout()


if __name__ == "__main__":
    main()
