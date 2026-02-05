#!/usr/bin/env python3
import argparse
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

try:
    import shioaji as sj
except Exception as exc:  # pragma: no cover - optional dependency in some envs
    raise SystemExit(f"shioaji not available: {exc}")


@dataclass
class ProbeResult:
    op: str
    count: int
    errors: int
    mean_ms: float
    std_ms: float
    p50_ms: float
    p90_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    jitter_mean_ms: float
    jitter_p95_ms: float


def _summary(op: str, samples_ms: list[float], errors: int) -> ProbeResult:
    if not samples_ms:
        return ProbeResult(
            op=op,
            count=0,
            errors=errors,
            mean_ms=0.0,
            std_ms=0.0,
            p50_ms=0.0,
            p90_ms=0.0,
            p95_ms=0.0,
            p99_ms=0.0,
            max_ms=0.0,
            jitter_mean_ms=0.0,
            jitter_p95_ms=0.0,
        )
    arr = np.asarray(samples_ms, dtype=np.float64)
    jitter = np.abs(np.diff(arr)) if arr.size > 1 else np.asarray([0.0], dtype=np.float64)
    return ProbeResult(
        op=op,
        count=int(arr.size),
        errors=errors,
        mean_ms=float(arr.mean()),
        std_ms=float(arr.std(ddof=0)),
        p50_ms=float(np.percentile(arr, 50)),
        p90_ms=float(np.percentile(arr, 90)),
        p95_ms=float(np.percentile(arr, 95)),
        p99_ms=float(np.percentile(arr, 99)),
        max_ms=float(arr.max()),
        jitter_mean_ms=float(jitter.mean()),
        jitter_p95_ms=float(np.percentile(jitter, 95)),
    )


def _time_call(fn: Callable[[], Any], samples_ms: list[float], errors: list[int]) -> None:
    start = time.perf_counter_ns()
    try:
        fn()
        elapsed_ms = (time.perf_counter_ns() - start) / 1e6
        samples_ms.append(elapsed_ms)
    except Exception:
        errors[0] += 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe Shioaji API latency and jitter.")
    parser.add_argument("--iters", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--sleep", type=float, default=0.2, help="sleep between calls (seconds)")
    parser.add_argument("--mode", choices=["sim", "real"], default=os.getenv("HFT_MODE", "sim"))
    parser.add_argument(
        "--order-mode",
        choices=["sim", "real", "inherit"],
        default=os.getenv("HFT_ORDER_MODE", "sim"),
        help="Order client mode (default: sim). inherit uses --mode client.",
    )
    parser.add_argument("--order-ca", action="store_true", help="Enable CA for order client in real mode")
    parser.add_argument("--symbol", default=os.getenv("HFT_SHIOAJI_PROBE_SYMBOL", "2330"))
    parser.add_argument("--futures", default=os.getenv("HFT_SHIOAJI_PROBE_FUT", "TXFC0"))
    parser.add_argument("--out-prefix", default="reports/shioaji_api_latency")
    parser.add_argument("--no-orders", action="store_true")
    parser.add_argument("--no-accounting", action="store_true")
    parser.add_argument("--no-market-data", action="store_true")
    args = parser.parse_args()

    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)

    simulation = args.mode == "sim"
    api = sj.Shioaji(simulation=simulation)

    api_key = os.getenv("SHIOAJI_API_KEY")
    secret = os.getenv("SHIOAJI_SECRET_KEY")
    if not api_key or not secret:
        raise SystemExit("Missing SHIOAJI_API_KEY/SHIOAJI_SECRET_KEY in environment")

    api.login(api_key=api_key, secret_key=secret, contracts_timeout=60000)

    if not simulation:
        ca_path = os.getenv("SHIOAJI_CA_PATH")
        ca_pass = os.getenv("SHIOAJI_CA_PASSWORD")
        if ca_path and ca_pass:
            api.activate_ca(ca_path=ca_path, ca_passwd=ca_pass)

    order_mode = args.order_mode
    if order_mode == "inherit":
        order_mode = args.mode
    order_sim = order_mode == "sim"
    order_api = sj.Shioaji(simulation=order_sim)
    order_api.login(api_key=api_key, secret_key=secret, contracts_timeout=60000)
    if order_mode == "real" and args.order_ca:
        ca_path = os.getenv("SHIOAJI_CA_PATH")
        ca_pass = os.getenv("SHIOAJI_CA_PASSWORD")
        if ca_path and ca_pass:
            order_api.activate_ca(ca_path=ca_path, ca_passwd=ca_pass)

    results: list[ProbeResult] = []

    if not args.no_accounting:
        accounting_ops: list[tuple[str, Callable[[], Any]]] = [
            ("account_balance", lambda: api.account_balance()),
            ("margin", lambda: api.margin(api.futopt_account)),
            ("list_positions_stock", lambda: api.list_positions(api.stock_account)),
            ("list_positions_futopt", lambda: api.list_positions(api.futopt_account)),
            ("list_position_detail", lambda: api.list_position_detail(api.stock_account)),
            (
                "list_profit_loss",
                lambda: api.list_profit_loss(api.stock_account, begin_date="2024-01-01", end_date="2024-01-02"),
            ),
        ]

        for name, fn in accounting_ops:
            samples: list[float] = []
            errors = [0]
            for _ in range(args.warmup):
                _time_call(fn, samples, errors)
                time.sleep(args.sleep)
            samples.clear()
            for _ in range(args.iters):
                _time_call(fn, samples, errors)
                time.sleep(args.sleep)
            results.append(_summary(name, samples, errors[0]))

    if not args.no_orders:
        stock_contract = order_api.Contracts.Stocks[args.symbol]
        stock_order = order_api.Order(
            price=1,
            quantity=1,
            action=sj.constant.Action.Buy,
            price_type=sj.constant.StockPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            order_lot=sj.constant.StockOrderLot.Common,
            account=order_api.stock_account,
        )

        def place_stock() -> Any:
            return order_api.place_order(stock_contract, stock_order)

        trade_holder: dict[str, Any] = {"trade": None}

        def place_stock_for_update() -> Any:
            trade_holder["trade"] = order_api.place_order(stock_contract, stock_order)

        def update_stock() -> Any:
            trade = trade_holder.get("trade")
            if trade is None:
                place_stock_for_update()
                trade = trade_holder["trade"]
            return order_api.update_order(trade=trade, price=1)

        def cancel_stock() -> Any:
            trade = trade_holder.get("trade")
            if trade is None:
                place_stock_for_update()
                trade = trade_holder["trade"]
            return order_api.cancel_order(trade)

        order_ops: list[tuple[str, Callable[[], Any]]] = [
            ("place_order_stock", place_stock),
            ("update_order_stock", update_stock),
            ("cancel_order_stock", cancel_stock),
        ]

        for name, fn in order_ops:
            samples: list[float] = []
            errors = [0]
            for _ in range(args.warmup):
                _time_call(fn, samples, errors)
                time.sleep(args.sleep)
            samples.clear()
            for _ in range(args.iters):
                _time_call(fn, samples, errors)
                time.sleep(args.sleep)
            results.append(_summary(name, samples, errors[0]))

        # Futures probe (optional)
        fut_contract = order_api.Contracts.Futures[args.futures]
        fut_order = order_api.Order(
            price=1,
            quantity=1,
            action=sj.constant.Action.Buy,
            price_type=sj.constant.FuturesPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            octype=sj.constant.FuturesOCType.Auto,
            account=order_api.futopt_account,
        )

        def place_fut() -> Any:
            return order_api.place_order(fut_contract, fut_order)

        samples = []
        errors = [0]
        for _ in range(args.warmup):
            _time_call(place_fut, samples, errors)
            time.sleep(args.sleep)
        samples.clear()
        for _ in range(args.iters):
            _time_call(place_fut, samples, errors)
            time.sleep(args.sleep)
        results.append(_summary("place_order_futures", samples, errors[0]))

    if not args.no_market_data:
        try:
            contract = api.Contracts.Stocks[args.symbol]
            market_ops: list[tuple[str, Callable[[], Any]]] = [
                ("snapshots", lambda: api.snapshots([contract])),
                ("ticks", lambda: api.ticks(contract, limit=5)),
                ("kbars", lambda: api.kbars(contract, start="2024-01-02", end="2024-01-03")),
            ]
            for name, fn in market_ops:
                samples: list[float] = []
                errors = [0]
                for _ in range(args.warmup):
                    _time_call(fn, samples, errors)
                    time.sleep(args.sleep)
                samples.clear()
                for _ in range(args.iters):
                    _time_call(fn, samples, errors)
                    time.sleep(args.sleep)
                results.append(_summary(name, samples, errors[0]))
        except Exception:
            results.append(_summary("market_data", [], 1))

    payload = [r.__dict__ for r in results]
    json_path = out_prefix.with_suffix(".json")
    csv_path = out_prefix.with_suffix(".csv")

    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8") as fh:
        fh.write(
            "op,count,errors,mean_ms,std_ms,p50_ms,p90_ms,p95_ms,p99_ms,max_ms,jitter_mean_ms,jitter_p95_ms\n"
        )
        for row in payload:
            fh.write(",".join(str(row[k]) for k in [
                "op",
                "count",
                "errors",
                "mean_ms",
                "std_ms",
                "p50_ms",
                "p90_ms",
                "p95_ms",
                "p99_ms",
                "max_ms",
                "jitter_mean_ms",
                "jitter_p95_ms",
            ]) + "\n")

    print(f"Wrote {json_path} and {csv_path}")


if __name__ == "__main__":
    main()
