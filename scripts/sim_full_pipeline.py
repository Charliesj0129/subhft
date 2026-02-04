#!/usr/bin/env python3
import asyncio
import os
import random
import socket
import tempfile
import time
from pathlib import Path

import yaml


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


def _write_symbols_yaml(symbol: str) -> str:
    tmp = tempfile.NamedTemporaryFile(prefix="hft_symbols_", suffix=".yaml", delete=False, mode="w")
    data = {
        "symbols": [
            {
                "code": symbol,
                "exchange": "TAIFEX",
                "product_type": "future",
                "tick_size": 1,
                "tags": ["futures", "sim"],
            }
        ]
    }
    yaml.safe_dump(data, tmp)
    tmp.close()
    return tmp.name


def _write_strategies_yaml(symbol: str, enabled: bool) -> str:
    tmp = tempfile.NamedTemporaryFile(prefix="hft_strategies_", suffix=".yaml", delete=False, mode="w")
    data = {
        "strategies": [
            {
                "id": "SIM_MM",
                "module": "hft_platform.strategies.simple_mm",
                "class": "SimpleMarketMaker",
                "enabled": enabled,
                "symbols": [symbol],
            }
        ]
    }
    yaml.safe_dump(data, tmp)
    tmp.close()
    return tmp.name


def _write_risk_yaml(max_price_cap: float) -> str:
    tmp = tempfile.NamedTemporaryFile(prefix="hft_risk_", suffix=".yaml", delete=False, mode="w")
    data = {
        "global_defaults": {
            "max_price_cap": float(max_price_cap),
            "max_notional": 100_000_000,
        },
        "strategies": {
            "SIM_MM": {
                "max_notional": 100_000_000,
            }
        },
    }
    yaml.safe_dump(data, tmp)
    tmp.close()
    return tmp.name


async def main():
    load_env()
    os.environ["HFT_MODE"] = "sim"
    sock = socket.socket()
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    os.environ["HFT_PROM_PORT"] = str(port)
    os.environ["SHIOAJI_ACTIVATE_CA"] = "0"
    os.environ["HFT_ACTIVATE_CA"] = "0"
    # Aggressive reconnect thresholds for verification
    os.environ["HFT_MD_RESUBSCRIBE_GAP_S"] = "1"
    os.environ["HFT_MD_RECONNECT_GAP_S"] = "2"
    os.environ["HFT_MD_FORCE_RECONNECT_GAP_S"] = "4"
    os.environ["HFT_MD_RECONNECT_COOLDOWN_S"] = "1"
    os.environ["HFT_RECONNECT_COOLDOWN"] = "1"
    os.environ["HFT_RECONNECT_BACKOFF_S"] = "1"
    os.environ["HFT_RECONNECT_BACKOFF_MAX_S"] = "4"
    os.environ["HFT_MD_SYNTHETIC_SIDE"] = "1"
    os.environ["SHIOAJI_FETCH_CONTRACT"] = "1"

    api_key = os.getenv("SHIOAJI_API_KEY") or os.getenv("SHIOAJI_APIKEY")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY") or os.getenv("SHIOAJI_SECRETKEY")
    if not api_key or not secret_key:
        print("Missing SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY in env.")
        return 1

    import shioaji as sj

    api = sj.Shioaji(simulation=True)
    api.login(api_key=api_key, secret_key=secret_key)
    try:
        api.fetch_contracts(contract_download=True)
    except Exception as exc:
        print(f"fetch_contracts_error={exc}")

    symbol, contract = _pick_contract(api, os.getenv("SHIOAJI_FUT_TEST_SYMBOL", "TXFC0"))
    if not contract:
        print("No futures contract available in simulation.")
        return 2

    ref_price = getattr(contract, "reference", None) or getattr(contract, "limit_down", None)
    if not ref_price or float(ref_price) <= 0:
        for attr in ("limit_up", "last_price", "close", "settlement", "prev_close", "avg_price"):
            val = getattr(contract, attr, None)
            if val and float(val) > 0:
                ref_price = val
                break
    if not ref_price or float(ref_price) <= 0:
        ref_price = 100.0

    symbols_path = _write_symbols_yaml(symbol)
    enable_orders = os.getenv("SIM_ENABLE_ORDERS", "0") == "1"
    strategies_path = _write_strategies_yaml(symbol, enabled=enable_orders)
    risk_path = _write_risk_yaml(max_price_cap=float(ref_price) * 2.0)
    temp_paths = [symbols_path, strategies_path, risk_path]

    os.environ["SYMBOLS_CONFIG"] = symbols_path
    os.environ["HFT_STRATEGY_CONFIG"] = strategies_path

    from hft_platform.config import loader
    from hft_platform.main import HFTSystem
    from prometheus_client import start_http_server

    settings, _ = loader.load_settings()
    settings.setdefault("paths", {})["strategy_limits"] = risk_path
    metrics_port = os.getenv("HFT_PROM_PORT")
    start_http_server(int(metrics_port))
    print(f"metrics_port={metrics_port}")
    system = HFTSystem(settings)
    task = asyncio.create_task(system.run())

    await asyncio.sleep(3)

    sim_duration_s = float(os.getenv("SIM_DURATION_S", "300"))
    lag_ms = float(os.getenv("SIM_EXCH_LAG_MS", "3.0"))
    jitter_ms = float(os.getenv("SIM_EXCH_JITTER_MS", "1.0"))
    start_ts = time.time()
    i = 0
    while time.time() - start_ts < sim_duration_s:
        base_price = float(ref_price)
        bid = max(base_price - 1.0, 1.0)
        ask = max(base_price + 1.0, bid + 1.0)
        lag_sample_ms = max(0.0, random.gauss(lag_ms, jitter_ms))
        lag_ns = int(lag_sample_ms * 1_000_000)
        payload = {
            "code": symbol,
            "ts": time.time_ns() - lag_ns,
            "bid_price": [bid],
            "bid_volume": [1],
            "ask_price": [ask],
            "ask_volume": [1],
        }
        await system.raw_queue.put(payload)
        await asyncio.sleep(0.05)
        i += 1

    await asyncio.sleep(0.5)
    # Validate LOB stats after synthetic side handling.
    book = system.md_service.lob.get_book(symbol)
    stats = book.get_stats()
    print(
        "lob_stats",
        f"mid={stats.mid_price}",
        f"spread={stats.spread}",
        f"best_bid={stats.best_bid}",
        f"best_ask={stats.best_ask}",
    )

    # Force a gap + resubscribe failure to validate reconnect logic.
    system.md_service.client.tick_callback = None
    system.md_service._resubscribe_attempts = 3
    system.md_service.last_event_ts = time.time() - 999

    await asyncio.sleep(6)

    live_orders = len(system.order_adapter.live_orders)
    print(f"sim_symbol={symbol}")
    print(f"sim_orders_live={live_orders}")

    try:
        import urllib.request

        port = os.getenv("HFT_PROM_PORT", "9102")
        metrics = urllib.request.urlopen(f"http://localhost:{port}/metrics", timeout=2).read().decode("utf-8")
        for key in (
            "execution_router_alive",
            "execution_gateway_alive",
            "queue_depth",
            "feed_resubscribe_total",
            "feed_reconnect_total",
        ):
            print(f"metrics_present_{key}={str(key in metrics).lower()}")
    except Exception as exc:
        print(f"metrics_check_error={exc}")

    system.stop()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    api.logout()
    for path in temp_paths:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
