#!/usr/bin/env python3
"""
High-frequency Order Flow Imbalance (OFI) forecasting sandbox.

- Simulate a discrete-tick LOB with limit/cancel/market events
- Compute Cont-style OFI from best bid/ask price+size series
- Fit a simple AR(L) model to forecast OFI at a horizon
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Dict, Tuple

import numpy as np


@dataclass
class SimConfig:
    steps: int = 20000
    dt: float = 0.01
    levels: int = 10
    base_depth: int = 50
    tick_size: int = 1
    mid_price: int = 1000
    seed: int = 42
    sample_every: int = 10
    depth_geo_p: float = 0.35
    rate_limit: float = 12.0
    rate_cancel: float = 9.0
    rate_market: float = 4.0
    size_dist: str = "poisson"
    size_param1: float = 8.0
    size_param2: float = 0.0


def _sample_size(rng: np.random.Generator, cfg: SimConfig) -> int:
    if cfg.size_dist == "poisson":
        size = int(rng.poisson(cfg.size_param1)) + 1
    elif cfg.size_dist == "geometric":
        size = int(rng.geometric(cfg.size_param1))
    elif cfg.size_dist == "lognormal":
        size = int(rng.lognormal(cfg.size_param1, cfg.size_param2))
    else:
        raise ValueError(f"unknown size_dist: {cfg.size_dist}")
    return max(1, size)


def _sample_level(rng: np.random.Generator, cfg: SimConfig) -> int:
    level = int(rng.geometric(cfg.depth_geo_p)) - 1
    return min(max(level, 0), cfg.levels - 1)


def _choose_cancel_level(rng: np.random.Generator, depth: np.ndarray) -> int:
    total = int(depth.sum())
    if total <= 0:
        return 0
    weights = depth / total
    return int(rng.choice(len(depth), p=weights))


def _consume_from_best(depth: np.ndarray, size: int, levels: int) -> int:
    remaining = size
    for level in range(levels):
        if remaining <= 0:
            break
        available = int(depth[level])
        if available <= 0:
            continue
        take = min(available, remaining)
        depth[level] -= take
        remaining -= take
    return size - remaining


def _best_price(depth: np.ndarray, side: str, mid: int, tick: int, levels: int) -> int:
    active = np.where(depth > 0)[0]
    level = int(active[0]) if active.size else levels - 1
    if side == "bid":
        return mid - tick * (1 + level)
    return mid + tick * (1 + level)


def simulate(cfg: SimConfig) -> Dict[str, np.ndarray]:
    rng = np.random.default_rng(cfg.seed)
    bid = np.full(cfg.levels, cfg.base_depth, dtype=np.int64)
    ask = np.full(cfg.levels, cfg.base_depth, dtype=np.int64)

    samples = cfg.steps // cfg.sample_every + 1
    times = np.zeros(samples, dtype=np.float64)
    best_bid = np.zeros(samples, dtype=np.int64)
    best_ask = np.zeros(samples, dtype=np.int64)
    best_bid_sz = np.zeros(samples, dtype=np.int64)
    best_ask_sz = np.zeros(samples, dtype=np.int64)

    best_bid[0] = _best_price(bid, "bid", cfg.mid_price, cfg.tick_size, cfg.levels)
    best_ask[0] = _best_price(ask, "ask", cfg.mid_price, cfg.tick_size, cfg.levels)
    best_bid_sz[0] = int(bid[0])
    best_ask_sz[0] = int(ask[0])

    s_idx = 1
    for step in range(1, cfg.steps + 1):
        # Limit orders
        for _ in range(rng.poisson(cfg.rate_limit * cfg.dt)):
            level = _sample_level(rng, cfg)
            size = _sample_size(rng, cfg)
            bid[level] += size
        for _ in range(rng.poisson(cfg.rate_limit * cfg.dt)):
            level = _sample_level(rng, cfg)
            size = _sample_size(rng, cfg)
            ask[level] += size

        # Cancellations
        for _ in range(rng.poisson(cfg.rate_cancel * cfg.dt)):
            level = _choose_cancel_level(rng, bid)
            size = _sample_size(rng, cfg)
            bid[level] -= min(size, int(bid[level]))
        for _ in range(rng.poisson(cfg.rate_cancel * cfg.dt)):
            level = _choose_cancel_level(rng, ask)
            size = _sample_size(rng, cfg)
            ask[level] -= min(size, int(ask[level]))

        # Market orders
        for _ in range(rng.poisson(cfg.rate_market * cfg.dt)):
            size = _sample_size(rng, cfg)
            _consume_from_best(ask, size, cfg.levels)
        for _ in range(rng.poisson(cfg.rate_market * cfg.dt)):
            size = _sample_size(rng, cfg)
            _consume_from_best(bid, size, cfg.levels)

        if bid.sum() == 0:
            bid[-1] = cfg.base_depth
        if ask.sum() == 0:
            ask[-1] = cfg.base_depth

        if step % cfg.sample_every == 0:
            times[s_idx] = step * cfg.dt
            best_bid[s_idx] = _best_price(bid, "bid", cfg.mid_price, cfg.tick_size, cfg.levels)
            best_ask[s_idx] = _best_price(ask, "ask", cfg.mid_price, cfg.tick_size, cfg.levels)
            best_bid_sz[s_idx] = int(bid[0])
            best_ask_sz[s_idx] = int(ask[0])
            s_idx += 1

    return {
        "time": times,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "best_bid_sz": best_bid_sz,
        "best_ask_sz": best_ask_sz,
    }


def compute_ofi(best_bid: np.ndarray, best_ask: np.ndarray, bid_sz: np.ndarray, ask_sz: np.ndarray) -> np.ndarray:
    n = best_bid.size
    ofi = np.zeros(n, dtype=np.float64)
    for t in range(1, n):
        # Bid flow
        if best_bid[t] > best_bid[t - 1]:
            bid_flow = bid_sz[t]
        elif best_bid[t] < best_bid[t - 1]:
            bid_flow = -bid_sz[t - 1]
        else:
            bid_flow = bid_sz[t] - bid_sz[t - 1]

        # Ask flow
        if best_ask[t] < best_ask[t - 1]:
            ask_flow = ask_sz[t]
        elif best_ask[t] > best_ask[t - 1]:
            ask_flow = -ask_sz[t - 1]
        else:
            ask_flow = ask_sz[t] - ask_sz[t - 1]

        ofi[t] = float(bid_flow - ask_flow)
    return ofi


def build_design(ofi: np.ndarray, lags: int, horizon: int) -> Tuple[np.ndarray, np.ndarray]:
    if lags <= 0:
        raise ValueError("lags must be > 0")
    if horizon <= 0:
        raise ValueError("horizon must be > 0")

    n = ofi.size
    rows = n - lags - horizon
    if rows <= 0:
        raise ValueError("not enough samples for lags+horizon")

    X = np.zeros((rows, lags + 1), dtype=np.float64)
    y = np.zeros(rows, dtype=np.float64)

    for i in range(rows):
        t = i + lags
        X[i, 0] = 1.0
        X[i, 1:] = ofi[t - lags : t][::-1]
        y[i] = ofi[t + horizon]
    return X, y


def fit_ols(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    return np.linalg.lstsq(X, y, rcond=None)[0]


def evaluate(pred: np.ndarray, target: np.ndarray) -> Dict[str, float]:
    err = pred - target
    rmse = float(np.sqrt(np.mean(err * err)))
    if pred.size < 2 or np.std(pred) == 0.0 or np.std(target) == 0.0:
        corr = float("nan")
    else:
        corr = float(np.corrcoef(pred, target)[0, 1])
    return {"rmse": rmse, "corr": corr}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OFI forecasting sandbox")
    parser.add_argument("--steps", type=int, default=20000)
    parser.add_argument("--dt", type=float, default=0.01)
    parser.add_argument("--levels", type=int, default=10)
    parser.add_argument("--base-depth", type=int, default=50)
    parser.add_argument("--tick-size", type=int, default=1)
    parser.add_argument("--mid-price", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sample-every", type=int, default=10)
    parser.add_argument("--depth-geo-p", type=float, default=0.35)
    parser.add_argument("--rate-limit", type=float, default=12.0)
    parser.add_argument("--rate-cancel", type=float, default=9.0)
    parser.add_argument("--rate-market", type=float, default=4.0)
    parser.add_argument("--size-dist", type=str, default="poisson")
    parser.add_argument("--size-param1", type=float, default=8.0)
    parser.add_argument("--size-param2", type=float, default=0.0)
    parser.add_argument("--lags", type=int, default=5)
    parser.add_argument("--horizon", type=int, default=1)
    parser.add_argument("--out", type=str, default="")
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> SimConfig:
    if args.steps <= 0:
        raise ValueError("steps must be > 0")
    if args.dt <= 0:
        raise ValueError("dt must be > 0")
    if args.levels < 2:
        raise ValueError("levels must be >= 2")
    if args.sample_every <= 0:
        raise ValueError("sample_every must be > 0")
    if args.depth_geo_p <= 0.0 or args.depth_geo_p >= 1.0:
        raise ValueError("depth_geo_p must be in (0, 1)")

    return SimConfig(
        steps=args.steps,
        dt=args.dt,
        levels=args.levels,
        base_depth=args.base_depth,
        tick_size=args.tick_size,
        mid_price=args.mid_price,
        seed=args.seed,
        sample_every=args.sample_every,
        depth_geo_p=args.depth_geo_p,
        rate_limit=args.rate_limit,
        rate_cancel=args.rate_cancel,
        rate_market=args.rate_market,
        size_dist=args.size_dist,
        size_param1=args.size_param1,
        size_param2=args.size_param2,
    )


def main() -> None:
    args = parse_args()
    cfg = build_config(args)
    data = simulate(cfg)
    ofi = compute_ofi(
        data["best_bid"], data["best_ask"], data["best_bid_sz"], data["best_ask_sz"]
    )

    X, y = build_design(ofi, args.lags, args.horizon)
    coef = fit_ols(X, y)
    pred = X @ coef

    stats = evaluate(pred, y)
    baseline = evaluate(np.zeros_like(y), y)

    print("OFI summary")
    print("ofi_mean:", round(float(ofi.mean()), 4))
    print("ofi_std:", round(float(ofi.std()), 4))
    print(f"lags={args.lags} horizon={args.horizon}")
    print("rmse:", round(stats["rmse"], 6), "corr:", round(stats["corr"], 6))
    print("baseline_rmse:", round(baseline["rmse"], 6))

    if args.out:
        np.savez(
            args.out,
            time=data["time"],
            best_bid=data["best_bid"],
            best_ask=data["best_ask"],
            best_bid_sz=data["best_bid_sz"],
            best_ask_sz=data["best_ask_sz"],
            ofi=ofi,
            X=X,
            y=y,
            coef=coef,
            pred=pred,
        )
        print(f"saved npz: {args.out}")


if __name__ == "__main__":
    main()
