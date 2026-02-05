#!/usr/bin/env python3
import argparse
import json
import os
import time
from datetime import datetime, timedelta
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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


def _run_ops(
    results: list[ProbeResult],
    ops: list[tuple[str, Callable[[], Any] | None]],
    iters: int,
    warmup: int,
    sleep_s: float,
) -> None:
    for name, fn in ops:
        if fn is None:
            results.append(_summary(name, [], 0))
            continue
        samples: list[float] = []
        errors = [0]
        for _ in range(warmup):
            _time_call(fn, samples, errors)
            time.sleep(sleep_s)
        samples.clear()
        for _ in range(iters):
            _time_call(fn, samples, errors)
            time.sleep(sleep_s)
        results.append(_summary(name, samples, errors[0]))


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
    parser.add_argument("--option", default=os.getenv("HFT_SHIOAJI_PROBE_OPT", "TXO22400B6"))
    parser.add_argument("--out-prefix", default="reports/shioaji_api_latency")
    parser.add_argument("--no-orders", action="store_true")
    parser.add_argument("--no-accounting", action="store_true")
    parser.add_argument("--no-market-data", action="store_true")
    parser.add_argument("--no-quotes", action="store_true")
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

    def _coerce_price(value: Any) -> int | None:
        if value is None:
            return None
        try:
            if hasattr(value, "item"):
                value = value.item()
            if isinstance(value, (list, tuple)):
                for item in value:
                    price = _coerce_price(item)
                    if price:
                        return price
                return None
            dec = Decimal(str(value))
            if dec <= 0:
                return None
            return int(dec.to_integral_value())
        except (InvalidOperation, ValueError, TypeError):
            return None

    def _snapshot_price(client: Any, contract: Any) -> int | None:
        try:
            snaps = client.snapshots([contract])
        except Exception:
            return None
        if not snaps:
            return None
        snap = snaps[0]
        if isinstance(snap, dict):
            data = snap
        elif hasattr(snap, "_asdict"):
            data = snap._asdict()
        elif hasattr(snap, "__dict__"):
            data = snap.__dict__
        else:
            data = {}
        for key in (
            "last_price",
            "last",
            "close",
            "price",
            "reference_price",
            "avg_price",
            "open",
            "bid_price",
            "ask_price",
        ):
            if key in data:
                price = _coerce_price(data[key])
                if price:
                    return price
        return None

    def _limit_price(client: Any, contract: Any, fallback: int = 1) -> int:
        price = _snapshot_price(client, contract)
        return price if price is not None else fallback

    def iter_contracts(container: Any):
        if container is None:
            return
        iterable = container.values() if isinstance(container, dict) else container
        for item in iterable:
            yield item
            try:
                if hasattr(item, "__iter__") and not hasattr(item, "code"):
                    for sub in item:
                        yield sub
            except Exception:
                continue

    def find_contract_by_code(container: Any, code: str | None, prefixes: tuple[str, ...]) -> Any | None:
        if container is None:
            return None
        if code:
            try:
                return container[code]
            except Exception:
                pass
            for contract in iter_contracts(container):
                if getattr(contract, "code", None) == code:
                    return contract
        for contract in iter_contracts(container):
            ccode = getattr(contract, "code", None)
            if not ccode:
                continue
            if ccode.startswith(prefixes):
                return contract
        return None

    def last_trading_date() -> str:
        tz_name = os.getenv("HFT_SHIOAJI_TZ", "Asia/Taipei")
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = None
        now = datetime.now(tz=tz)
        day = now.date()
        # Roll back to last weekday if weekend
        while day.weekday() >= 5:
            day -= timedelta(days=1)
        return day.strftime("%Y-%m-%d")

    if not args.no_accounting:
        accounting_ops: list[tuple[str, Callable[[], Any] | None]] = [
            ("list_accounts", (lambda: api.list_accounts()) if hasattr(api, "list_accounts") else None),
            ("account_balance", lambda: api.account_balance()),
            ("margin", lambda: api.margin(api.futopt_account)),
            ("list_positions_stock", lambda: api.list_positions(api.stock_account)),
            ("list_positions_futopt", lambda: api.list_positions(api.futopt_account)),
            ("list_position_detail", lambda: api.list_position_detail(api.stock_account)),
            (
                "list_profit_loss",
                lambda: api.list_profit_loss(api.stock_account, begin_date="2024-01-01", end_date="2024-01-02"),
            ),
            ("update_status", (lambda: api.update_status(api.stock_account)) if hasattr(api, "update_status") else None),
            ("list_trades", (lambda: api.list_trades()) if hasattr(api, "list_trades") else None),
            ("usage", (lambda: api.usage()) if hasattr(api, "usage") else None),
        ]

        _run_ops(results, accounting_ops, args.iters, args.warmup, args.sleep)

    if not args.no_orders:
        stock_contract = find_contract_by_code(order_api.Contracts.Stocks, args.symbol, ("",))
        stock_price = _limit_price(order_api, stock_contract, fallback=1)
        stock_order = order_api.Order(
            price=stock_price,
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
            return order_api.update_order(trade=trade, price=stock_price)

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

        _run_ops(results, order_ops, args.iters, args.warmup, args.sleep)

        # Futures probe (optional)
        fut_prefix = os.getenv("HFT_SHIOAJI_PROBE_FUT_PREFIX", "TXF")
        fut_contract = find_contract_by_code(order_api.Contracts.Futures, args.futures, (fut_prefix, "MXF", "TMF"))

        if fut_contract is not None:
            fut_price = _limit_price(order_api, fut_contract, fallback=1)
            fut_order = order_api.Order(
                price=fut_price,
                quantity=1,
                action=sj.constant.Action.Buy,
                price_type=sj.constant.FuturesPriceType.LMT,
                order_type=sj.constant.OrderType.ROD,
                octype=sj.constant.FuturesOCType.Auto,
                account=order_api.futopt_account,
            )

            def place_fut() -> Any:
                return order_api.place_order(fut_contract, fut_order)

            _run_ops(results, [("place_order_futures", place_fut)], args.iters, args.warmup, args.sleep)
        else:
            results.append(_summary("place_order_futures", [], 0))

    if not args.no_market_data:
        stock_contract = find_contract_by_code(api.Contracts.Stocks, args.symbol, ("",))
        fut_prefix = os.getenv("HFT_SHIOAJI_PROBE_FUT_PREFIX", "TXF")
        fut_contract = find_contract_by_code(api.Contracts.Futures, args.futures, (fut_prefix, "MXF", "TMF"))
        opt_prefix = os.getenv("HFT_SHIOAJI_PROBE_OPT_PREFIX", "TXO")
        opt_contract = find_contract_by_code(api.Contracts.Options, args.option, (opt_prefix,))
        trade_date = last_trading_date()

        market_ops: list[tuple[str, Callable[[], Any] | None]] = []
        if stock_contract is not None:
            market_ops.extend(
                [
                    ("snapshots_stock", lambda: api.snapshots([stock_contract])),
                    ("ticks_stock", lambda: api.ticks(stock_contract, date=trade_date)),
                    ("kbars_stock", lambda: api.kbars(stock_contract, start=trade_date, end=trade_date)),
                ]
            )
        else:
            market_ops.append(("snapshots_stock", None))

        if fut_contract is not None:
            market_ops.extend(
                [
                    ("snapshots_futures", lambda: api.snapshots([fut_contract])),
                    ("ticks_futures", lambda: api.ticks(fut_contract, date=trade_date)),
                    ("kbars_futures", lambda: api.kbars(fut_contract, start=trade_date, end=trade_date)),
                ]
            )
        else:
            market_ops.append(("snapshots_futures", None))

        if opt_contract is not None:
            market_ops.append(("snapshots_options", lambda: api.snapshots([opt_contract])))
        else:
            market_ops.append(("snapshots_options", None))

        _run_ops(results, market_ops, args.iters, args.warmup, args.sleep)

    if not args.no_quotes:
        quote_ops: list[tuple[str, Callable[[], Any] | None]] = []
        try:
            quote_contract = api.Contracts.Stocks[args.symbol]
        except Exception:
            quote_contract = None
        if quote_contract is not None and hasattr(api, "quote"):
            for qt in (sj.constant.QuoteType.Tick, sj.constant.QuoteType.BidAsk):
                try:
                    api.quote.unsubscribe(quote_contract, quote_type=qt)
                except Exception:
                    pass
            sub_state = {"tick": False, "bidask": False}

            def _quote_sub_tick() -> Any:
                if sub_state["tick"]:
                    api.quote.unsubscribe(quote_contract, quote_type=sj.constant.QuoteType.Tick)
                    sub_state["tick"] = False
                result = api.quote.subscribe(quote_contract, quote_type=sj.constant.QuoteType.Tick)
                sub_state["tick"] = True
                return result

            def _quote_sub_ba() -> Any:
                if sub_state["bidask"]:
                    api.quote.unsubscribe(quote_contract, quote_type=sj.constant.QuoteType.BidAsk)
                    sub_state["bidask"] = False
                result = api.quote.subscribe(quote_contract, quote_type=sj.constant.QuoteType.BidAsk)
                sub_state["bidask"] = True
                return result

            def _quote_unsub_tick() -> Any:
                if sub_state["tick"]:
                    result = api.quote.unsubscribe(quote_contract, quote_type=sj.constant.QuoteType.Tick)
                    sub_state["tick"] = False
                    return result
                return None

            def _quote_unsub_ba() -> Any:
                if sub_state["bidask"]:
                    result = api.quote.unsubscribe(quote_contract, quote_type=sj.constant.QuoteType.BidAsk)
                    sub_state["bidask"] = False
                    return result
                return None

            quote_ops.extend(
                [
                    ("quote_sub_tick", _quote_sub_tick),
                    ("quote_sub_bidask", _quote_sub_ba),
                    ("quote_unsub_tick", _quote_unsub_tick),
                    ("quote_unsub_bidask", _quote_unsub_ba),
                ]
            )
        else:
            quote_ops.append(("quote_sub_tick", None))
            quote_ops.append(("quote_sub_bidask", None))
            quote_ops.append(("quote_unsub_tick", None))
            quote_ops.append(("quote_unsub_bidask", None))

        _run_ops(results, quote_ops, args.iters, args.warmup, args.sleep)

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
