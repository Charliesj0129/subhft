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


def _safe_len(value) -> int:
    try:
        return len(value)
    except Exception:
        try:
            return len([x for x in value])
        except Exception:
            return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--contracts-path",
        dest="contracts_path",
        default=str(Path(".shioaji_contracts").resolve()),
    )
    args = parser.parse_args()

    load_env()
    os.environ["SJ_CONTRACTS_PATH"] = args.contracts_path
    os.environ.setdefault("SHIOAJI_ACTIVATE_CA", "0")
    os.environ.setdefault("HFT_ACTIVATE_CA", "0")

    api_key = os.getenv("SHIOAJI_API_KEY") or os.getenv("SHIOAJI_APIKEY")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY") or os.getenv("SHIOAJI_SECRETKEY")
    if not api_key or not secret_key:
        print("Missing SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY in env.")
        return 1

    Path(args.contracts_path).mkdir(parents=True, exist_ok=True)

    import shioaji as sj

    api = sj.Shioaji(simulation=False)
    api.login(
        api_key=api_key,
        secret_key=secret_key,
        contracts_timeout=int(os.getenv("SHIOAJI_CONTRACTS_TIMEOUT", "10000")),
        fetch_contract=True,
        subscribe_trade=False,
    )
    api.fetch_contracts(contract_download=True)

    stocks_total = _safe_len(getattr(api.Contracts, "Stocks", []))
    futures_total = _safe_len(getattr(api.Contracts, "Futures", []))
    options_total = _safe_len(getattr(api.Contracts, "Options", []))

    print(f"contracts_path={args.contracts_path}")
    print(f"stocks_total={stocks_total}")
    print(f"futures_total={futures_total}")
    print(f"options_total={options_total}")

    api.logout()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
