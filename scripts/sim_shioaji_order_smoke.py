#!/usr/bin/env python3
import os
import sys
from pathlib import Path

import shioaji as sj


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
    load_env()
    api_key = os.getenv("SHIOAJI_API_KEY") or os.getenv("SHIOAJI_APIKEY")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY") or os.getenv("SHIOAJI_SECRETKEY")

    if not api_key or not secret_key:
        print("Missing SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY in env.")
        sys.exit(1)

    api = sj.Shioaji(simulation=True)
    api.login(api_key=api_key, secret_key=secret_key)
    api.fetch_contracts(contract_download=True)

    preferred = os.getenv("SHIOAJI_TEST_SYMBOL", "2330")
    fallback = ["2330", "2317", "2303", "2891", "2881", "0050", "1101"]
    symbols = [preferred] + [s for s in fallback if s != preferred]

    trade = None
    status_code = ""
    op_code = ""
    status_msg = ""
    status_state = ""

    def extract_op_code(trade_obj):
        op = getattr(trade_obj, "operation", None)
        if isinstance(op, dict):
            return op.get("op_code", "")
        return getattr(op, "op_code", "")
    chosen = None
    for symbol in symbols:
        try:
            contract = api.Contracts.Stocks[symbol]
        except Exception:
            continue
        order = api.Order(
            price=contract.reference,
            quantity=1,
            action=sj.constant.Action.Buy,
            price_type=sj.constant.StockPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            order_cond=sj.constant.StockOrderCond.Cash,
            order_lot=sj.constant.StockOrderLot.Common,
            account=api.stock_account,
        )
        trade = api.place_order(contract, order)
        status_code = getattr(trade.status, "status_code", "")
        status_state = str(getattr(trade.status, "status", ""))
        status_msg = getattr(trade.status, "msg", "")
        op_code = extract_op_code(trade)
        chosen = f"stock:{symbol}"
        if op_code != "88" and "Failed" not in status_state and "無此商品代碼" not in str(status_msg):
            break

    if trade is not None and (op_code == "88" or "Failed" in status_state or "無此商品代碼" in str(status_msg)):
        # Try futures current-month symbol in simulation
        for fut_symbol in ("TXFC0", "MXFC0", "TXF", "MXF"):
            try:
                fut_contract = api.Contracts.Futures[fut_symbol]
            except Exception:
                continue
            fut_order = api.Order(
                price=fut_contract.reference,
                quantity=1,
                action=sj.constant.Action.Buy,
                price_type=sj.constant.FuturesPriceType.LMT,
                order_type=sj.constant.OrderType.ROD,
                account=api.futopt_account,
            )
            trade = api.place_order(fut_contract, fut_order)
            status_code = getattr(trade.status, "status_code", "")
            status_state = str(getattr(trade.status, "status", ""))
            status_msg = getattr(trade.status, "msg", "")
            op_code = extract_op_code(trade)
            chosen = f"future:{fut_symbol}"
            if op_code != "88" and "Failed" not in status_state and "無此商品代碼" not in str(status_msg):
                break

    api.update_status(api.stock_account)
    trades = api.list_trades()

    op_obj = getattr(trade, "operation", None) if trade is not None else None
    op_obj_type = type(op_obj).__name__
    op_obj_code = op_obj.get("op_code") if isinstance(op_obj, dict) else getattr(op_obj, "op_code", "")

    print(f"order_target={chosen}")
    print(f"op_code={op_code}")
    print(f"status_code={status_code}")
    print(f"status_state={status_state}")
    print(f"status_msg={status_msg}")
    print(f"operation_type={op_obj_type}")
    print(f"operation_op_code={op_obj_code}")
    print(f"trades_count={len(trades)}")

    api.logout()


if __name__ == "__main__":
    main()
