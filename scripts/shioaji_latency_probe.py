#!/usr/bin/env python3
import argparse
import json
import os
import statistics
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


def _percentile(sorted_vals, q):
    if not sorted_vals:
        return None
    if q <= 0:
        return sorted_vals[0]
    if q >= 100:
        return sorted_vals[-1]
    k = (len(sorted_vals) - 1) * (q / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    d0 = sorted_vals[f] * (c - k)
    d1 = sorted_vals[c] * (k - f)
    return d0 + d1


def _stats(samples_ms):
    if not samples_ms:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "stdev": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "p99": None,
            "jitter_p99_p50": None,
            "jitter_p95_p50": None,
        }
    vals = sorted(samples_ms)
    mean = statistics.fmean(vals)
    stdev = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    p50 = _percentile(vals, 50)
    p95 = _percentile(vals, 95)
    p99 = _percentile(vals, 99)
    return {
        "count": len(vals),
        "min": vals[0],
        "max": vals[-1],
        "mean": mean,
        "stdev": stdev,
        "p50": p50,
        "p90": _percentile(vals, 90),
        "p95": p95,
        "p99": p99,
        "jitter_p99_p50": (p99 - p50) if p99 is not None and p50 is not None else None,
        "jitter_p95_p50": (p95 - p50) if p95 is not None and p50 is not None else None,
    }


def _timed_call(fn):
    start = time.perf_counter_ns()
    try:
        out = fn()
        ok = True
    except Exception as exc:  # noqa: BLE001
        out = exc
        ok = False
    dur_ms = (time.perf_counter_ns() - start) / 1e6
    return ok, dur_ms, out


def _repeat(label, fn, iterations, sleep_s):
    samples = []
    errors = 0
    for _ in range(iterations):
        ok, dur_ms, _ = _timed_call(fn)
        samples.append(dur_ms)
        if not ok:
            errors += 1
        time.sleep(sleep_s)
    return {"label": label, "stats": _stats(samples), "errors": errors, "samples_ms": samples}


def _pick_futures_contract(api, preferred: str):
    if preferred:
        try:
            return api.Contracts.Futures[preferred]
        except Exception:
            pass
    try:
        if hasattr(api.Contracts.Futures, "keys"):
            for key in api.Contracts.Futures.keys():
                try:
                    return api.Contracts.Futures[key]
                except Exception:
                    continue
    except Exception:
        pass
    try:
        for item in api.Contracts.Futures:
            return item
    except Exception:
        pass
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--sleep-ms", type=float, default=200)
    parser.add_argument("--contracts-path", default="")
    parser.add_argument("--stock-symbol", default="2330")
    parser.add_argument("--futures-symbol", default="TXFC0")
    parser.add_argument("--out", default="reports/shioaji_latency_probe.json")
    args = parser.parse_args()

    load_env()
    if args.contracts_path:
        os.environ["SJ_CONTRACTS_PATH"] = args.contracts_path

    import shioaji as sj

    api_key = os.getenv("SHIOAJI_API_KEY") or os.getenv("SHIOAJI_APIKEY")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY") or os.getenv("SHIOAJI_SECRETKEY")
    if not api_key or not secret_key:
        raise SystemExit("Missing SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY in env.")

    api = sj.Shioaji(simulation=True)
    report = {
        "mode": "simulation",
        "iterations": args.iterations,
        "sleep_ms": args.sleep_ms,
        "ops": {},
    }

    ok, dur_ms, login_res = _timed_call(lambda: api.login(api_key=api_key, secret_key=secret_key))
    report["ops"]["login"] = {
        "stats": _stats([dur_ms]),
        "errors": 0 if ok else 1,
    }

    ok, dur_ms, _ = _timed_call(lambda: api.fetch_contracts(contract_download=True))
    report["ops"]["fetch_contracts"] = {
        "stats": _stats([dur_ms]),
        "errors": 0 if ok else 1,
    }

    stock_contract = None
    try:
        stock_contract = api.Contracts.Stocks[args.stock_symbol]
    except Exception:
        stock_contract = None

    fut_contract = _pick_futures_contract(api, args.futures_symbol)

    sleep_s = args.sleep_ms / 1000.0
    if stock_contract is not None:
        report["ops"]["snapshots_stock"] = _repeat(
            "snapshots_stock",
            lambda: api.snapshots([stock_contract]),
            args.iterations,
            sleep_s,
        )
    if fut_contract is not None:
        report["ops"]["snapshots_futures"] = _repeat(
            "snapshots_futures",
            lambda: api.snapshots([fut_contract]),
            args.iterations,
            sleep_s,
        )

    stock_account = api.stock_account
    fut_account = api.futopt_account

    if stock_account is not None:
        report["ops"]["update_status_stock"] = _repeat(
            "update_status_stock",
            lambda: api.update_status(stock_account),
            max(5, args.iterations // 3),
            sleep_s,
        )
    if fut_account is not None:
        report["ops"]["update_status_futures"] = _repeat(
            "update_status_futures",
            lambda: api.update_status(fut_account),
            max(5, args.iterations // 3),
            sleep_s,
        )

    report["ops"]["list_positions"] = _repeat(
        "list_positions",
        lambda: api.list_positions(api.stock_account or api.futopt_account),
        max(5, args.iterations // 3),
        sleep_s,
    )
    report["ops"]["list_profit_loss"] = _repeat(
        "list_profit_loss",
        lambda: api.list_profit_loss(api.stock_account or api.futopt_account),
        max(5, args.iterations // 3),
        sleep_s,
    )

    if fut_contract is not None and fut_account is not None:
        order = api.Order(
            price=getattr(fut_contract, "reference", 1) or 1,
            quantity=1,
            action=sj.constant.Action.Buy,
            price_type=sj.constant.FuturesPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            octype=sj.constant.FuturesOCType.Auto,
            account=fut_account,
        )
        ok, dur_ms, trade = _timed_call(lambda: api.place_order(fut_contract, order))
        report["ops"]["place_order_futures"] = {
            "stats": _stats([dur_ms]),
            "errors": 0 if ok else 1,
        }
        if ok:
            # Try update order price (may fail in sim); record latency either way.
            ok_u, dur_u, _ = _timed_call(
                lambda: api.update_order(trade=trade, price=order.price)
                if hasattr(api, "update_order")
                else api.update_price(trade=trade, price=order.price)
            )
            report["ops"]["update_order_futures"] = {
                "stats": _stats([dur_u]),
                "errors": 0 if ok_u else 1,
            }

            ok_c, dur_c, _ = _timed_call(lambda: api.cancel_order(trade))
            report["ops"]["cancel_order_futures"] = {
                "stats": _stats([dur_c]),
                "errors": 0 if ok_c else 1,
            }
    elif stock_contract is not None and stock_account is not None:
        order = api.Order(
            price=getattr(stock_contract, "reference", 1) or 1,
            quantity=1,
            action=sj.constant.Action.Buy,
            price_type=sj.constant.StockPriceType.LMT,
            order_type=sj.constant.OrderType.ROD,
            order_cond=sj.constant.StockOrderCond.Cash,
            order_lot=sj.constant.StockOrderLot.Common,
            account=stock_account,
        )
        ok, dur_ms, trade = _timed_call(lambda: api.place_order(stock_contract, order))
        report["ops"]["place_order_stock"] = {
            "stats": _stats([dur_ms]),
            "errors": 0 if ok else 1,
        }
        if ok:
            ok_u, dur_u, _ = _timed_call(
                lambda: api.update_order(trade=trade, price=order.price)
                if hasattr(api, "update_order")
                else api.update_price(trade=trade, price=order.price)
            )
            report["ops"]["update_order_stock"] = {
                "stats": _stats([dur_u]),
                "errors": 0 if ok_u else 1,
            }

            ok_c, dur_c, _ = _timed_call(lambda: api.cancel_order(trade))
            report["ops"]["cancel_order_stock"] = {
                "stats": _stats([dur_c]),
                "errors": 0 if ok_c else 1,
            }

    api.logout()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
