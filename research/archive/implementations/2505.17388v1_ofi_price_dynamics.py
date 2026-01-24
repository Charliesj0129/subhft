#!/usr/bin/env python3
"""
OFI-driven price dynamics prototype (OU response to OFI shocks).

- OFI generated from a simple LOB event simulation
- Mid-price updated in ticks with mean reversion + OFI shock
- Outputs impulse response and decay metrics
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Dict

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
    # Price dynamics
    kappa: float = 0.2
    ofi_beta: float = 0.04
    noise_std: float = 0.2


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


def simulate_ofi(cfg: SimConfig) -> Dict[str, np.ndarray]:
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
        for _ in range(rng.poisson(cfg.rate_limit * cfg.dt)):
            level = _sample_level(rng, cfg)
            size = _sample_size(rng, cfg)
            bid[level] += size
        for _ in range(rng.poisson(cfg.rate_limit * cfg.dt)):
            level = _sample_level(rng, cfg)
            size = _sample_size(rng, cfg)
            ask[level] += size

        for _ in range(rng.poisson(cfg.rate_cancel * cfg.dt)):
            level = _choose_cancel_level(rng, bid)
            size = _sample_size(rng, cfg)
            bid[level] -= min(size, int(bid[level]))
        for _ in range(rng.poisson(cfg.rate_cancel * cfg.dt)):
            level = _choose_cancel_level(rng, ask)
            size = _sample_size(rng, cfg)
            ask[level] -= min(size, int(ask[level]))

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
        if best_bid[t] > best_bid[t - 1]:
            bid_flow = bid_sz[t]
        elif best_bid[t] < best_bid[t - 1]:
            bid_flow = -bid_sz[t - 1]
        else:
            bid_flow = bid_sz[t] - bid_sz[t - 1]

        if best_ask[t] < best_ask[t - 1]:
            ask_flow = ask_sz[t]
        elif best_ask[t] > best_ask[t - 1]:
            ask_flow = -ask_sz[t - 1]
        else:
            ask_flow = ask_sz[t] - ask_sz[t - 1]

        ofi[t] = float(bid_flow - ask_flow)
    return ofi


def simulate_price(cfg: SimConfig, ofi: np.ndarray) -> Dict[str, np.ndarray]:
    rng = np.random.default_rng(cfg.seed + 99)
    n = ofi.size
    price = np.zeros(n, dtype=np.int64)
    price[0] = cfg.mid_price
    reversion_target = cfg.mid_price

    for t in range(1, n):
        drift = cfg.kappa * (reversion_target - price[t - 1]) * cfg.dt
        shock = cfg.ofi_beta * ofi[t]
        noise = rng.normal(0.0, cfg.noise_std)
        delta = drift + shock + noise
        ticks = int(np.round(delta))
        price[t] = price[t - 1] + ticks * cfg.tick_size
    return {"price": price}


def impulse_response(ofi: np.ndarray, price: np.ndarray, horizon: int) -> np.ndarray:
    responses = np.zeros(horizon + 1, dtype=np.float64)
    count = 0
    idx = np.where(np.abs(ofi) > 0)[0]
    if idx.size == 0:
        return responses
    for t in idx:
        if t + horizon >= price.size:
            continue
        base = price[t]
        for h in range(horizon + 1):
            responses[h] += price[t + h] - base
        count += 1
    if count > 0:
        responses /= count
    return responses


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OFI-driven price dynamics")
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
    parser.add_argument("--kappa", type=float, default=0.2)
    parser.add_argument("--ofi-beta", type=float, default=0.04)
    parser.add_argument("--noise-std", type=float, default=0.2)
    parser.add_argument("--ir-horizon", type=int, default=20)
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
    if args.tick_size <= 0:
        raise ValueError("tick_size must be > 0")

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
        kappa=args.kappa,
        ofi_beta=args.ofi_beta,
        noise_std=args.noise_std,
    )


def main() -> None:
    args = parse_args()
    cfg = build_config(args)

    ofi_data = simulate_ofi(cfg)
    ofi = compute_ofi(
        ofi_data["best_bid"],
        ofi_data["best_ask"],
        ofi_data["best_bid_sz"],
        ofi_data["best_ask_sz"],
    )
    price = simulate_price(cfg, ofi)["price"]
    ir = impulse_response(ofi, price, args.ir_horizon)

    print("OFI price dynamics summary")
    print("ofi_mean:", round(float(ofi.mean()), 4))
    print("ofi_std:", round(float(ofi.std()), 4))
    print("price_mean:", round(float(price.mean()), 4))
    print("price_std:", round(float(price.std()), 4))
    print("ir0:", round(float(ir[0]), 6), "irH:", round(float(ir[-1]), 6))

    if args.out:
        np.savez(
            args.out,
            time=ofi_data["time"],
            best_bid=ofi_data["best_bid"],
            best_ask=ofi_data["best_ask"],
            best_bid_sz=ofi_data["best_bid_sz"],
            best_ask_sz=ofi_data["best_ask_sz"],
            ofi=ofi,
            price=price,
            ir=ir,
        )
        print(f"saved npz: {args.out}")


if __name__ == "__main__":
    main()
