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

    fut_symbols = [
        os.getenv("SHIOAJI_FUT_TEST_SYMBOL", "TXFC0"),
        "MXFC0",
        "TXF",
        "MXF",
    ]

    trade = None
    chosen = None
    status_code = ""
    for symbol in fut_symbols:
        try:
            contract = api.Contracts.Futures[symbol]
        except Exception:
            continue
        if contract is None:
            continue
        if not hasattr(contract, "reference"):
            resolved = None
            try:
                for item in contract:
                    if hasattr(item, "reference"):
                        resolved = item
                        break
            except Exception:
                resolved = None
            contract = resolved
            if contract is None:
                continue
        order = api.Order(
            price=contract.reference,
            quantity=1,
            action=sj.constant.Action.Buy,
            price_type=sj.constant.FuturesPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            account=api.futopt_account,
        )
        trade = api.place_order(contract, order)
        chosen = f"future:{symbol}"
        status_code = getattr(trade.status, "status_code", "")
        break

    if trade is not None:
        api.update_status(api.futopt_account)
        trades = api.list_trades()
        print(f"order_target={chosen}")
        print(f"status_code={status_code}")
        print(f"trades_count={len(trades)}")
    else:
        symbols = []
        try:
            if hasattr(api.Contracts.Futures, "keys"):
                symbols = list(api.Contracts.Futures.keys())
            else:
                symbols = [c.code for c in api.Contracts.Futures if hasattr(c, "code")]
        except Exception:
            symbols = []
        print(f"order_target=None")
        print(f"available_futures={symbols[:20]}")

    api.logout()


if __name__ == "__main__":
    main()
