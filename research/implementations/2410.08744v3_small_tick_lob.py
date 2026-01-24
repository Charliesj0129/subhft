#!/usr/bin/env python3
"""
Small tick-size LOB modeling prototype.

- Simulate the same order-flow seed across multiple tick-size regimes
- Compare spread, depth profiles, and order flow imbalance (OFI-like)
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np


@dataclass
class SimConfig:
    steps: int = 20000
    dt: float = 0.01
    levels: int = 10
    base_depth: int = 50
    mid_price: int = 1000
    seed: int = 42
    sample_every: int = 20
    max_events: int = 20000
    depth_geo_p: float = 0.35
    # Event rates
    rate_limit: float = 12.0
    rate_cancel: float = 9.0
    rate_market: float = 4.0
    # Size distribution
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


def simulate_tick(cfg: SimConfig, tick: int, seed: int) -> Dict[str, np.ndarray]:
    rng = np.random.default_rng(seed)
    bid = np.full(cfg.levels, cfg.base_depth, dtype=np.int64)
    ask = np.full(cfg.levels, cfg.base_depth, dtype=np.int64)

    samples = cfg.steps // cfg.sample_every + 1
    times = np.zeros(samples, dtype=np.float64)
    spread = np.zeros(samples, dtype=np.int64)
    obi = np.zeros(samples, dtype=np.float64)
    depth_profile = np.zeros((samples, cfg.levels), dtype=np.float64)

    best_bid = _best_price(bid, "bid", cfg.mid_price, tick, cfg.levels)
    best_ask = _best_price(ask, "ask", cfg.mid_price, tick, cfg.levels)
    spread[0] = best_ask - best_bid
    total0 = float(bid.sum() + ask.sum())
    obi[0] = (bid.sum() - ask.sum()) / total0 if total0 > 0 else 0.0
    depth_profile[0] = (bid + ask) / 2.0

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
            cancel = min(size, int(bid[level]))
            bid[level] -= cancel
        for _ in range(rng.poisson(cfg.rate_cancel * cfg.dt)):
            level = _choose_cancel_level(rng, ask)
            size = _sample_size(rng, cfg)
            cancel = min(size, int(ask[level]))
            ask[level] -= cancel

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
            best_bid = _best_price(bid, "bid", cfg.mid_price, tick, cfg.levels)
            best_ask = _best_price(ask, "ask", cfg.mid_price, tick, cfg.levels)
            spread[s_idx] = best_ask - best_bid
            total = float(bid.sum() + ask.sum())
            obi[s_idx] = (bid.sum() - ask.sum()) / total if total > 0 else 0.0
            depth_profile[s_idx] = (bid + ask) / 2.0
            s_idx += 1

    return {
        "time": times,
        "spread": spread,
        "obi": obi,
        "depth_profile": depth_profile,
    }


def summarize(data: Dict[str, np.ndarray]) -> Dict[str, float]:
    spread = data["spread"]
    obi = data["obi"]
    depth_profile = data["depth_profile"]
    return {
        "spread_mean": float(spread.mean()),
        "spread_p95": float(np.percentile(spread, 95)),
        "obi_mean": float(obi.mean()),
        "obi_std": float(obi.std()),
        "depth_p50_lvl0": float(np.percentile(depth_profile[:, 0], 50)),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Small tick-size LOB modeling")
    parser.add_argument("--ticks", type=str, default="1,2,5,10")
    parser.add_argument("--steps", type=int, default=20000)
    parser.add_argument("--dt", type=float, default=0.01)
    parser.add_argument("--levels", type=int, default=10)
    parser.add_argument("--base-depth", type=int, default=50)
    parser.add_argument("--mid-price", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sample-every", type=int, default=20)
    parser.add_argument("--max-events", type=int, default=20000)
    parser.add_argument("--depth-geo-p", type=float, default=0.35)
    parser.add_argument("--rate-limit", type=float, default=12.0)
    parser.add_argument("--rate-cancel", type=float, default=9.0)
    parser.add_argument("--rate-market", type=float, default=4.0)
    parser.add_argument("--size-dist", type=str, default="poisson")
    parser.add_argument("--size-param1", type=float, default=8.0)
    parser.add_argument("--size-param2", type=float, default=0.0)
    parser.add_argument("--out-prefix", type=str, default="")
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
    ticks = [int(t.strip()) for t in args.ticks.split(",") if t.strip()]
    if not ticks:
        raise ValueError("ticks must contain at least one integer")

    cfg = SimConfig(
        steps=args.steps,
        dt=args.dt,
        levels=args.levels,
        base_depth=args.base_depth,
        mid_price=args.mid_price,
        seed=args.seed,
        sample_every=args.sample_every,
        max_events=args.max_events,
        depth_geo_p=args.depth_geo_p,
        rate_limit=args.rate_limit,
        rate_cancel=args.rate_cancel,
        rate_market=args.rate_market,
        size_dist=args.size_dist,
        size_param1=args.size_param1,
        size_param2=args.size_param2,
    )
    return cfg, ticks


def main() -> None:
    args = parse_args()
    cfg, ticks = build_config(args)
    results: List[Tuple[int, Dict[str, float]]] = []

    for tick in ticks:
        data = simulate_tick(cfg, tick, cfg.seed + tick)
        stats = summarize(data)
        results.append((tick, stats))

        print(f"tick={tick} summary")
        print("spread_mean:", round(stats["spread_mean"], 4))
        print("spread_p95:", round(stats["spread_p95"], 4))
        print("obi_mean:", round(stats["obi_mean"], 6))
        print("obi_std:", round(stats["obi_std"], 6))
        print("depth_p50_lvl0:", round(stats["depth_p50_lvl0"], 4))

        if args.out_prefix:
            out_path = f"{args.out_prefix}_tick{tick}.npz"
            np.savez(
                out_path,
                time=data["time"],
                spread=data["spread"],
                obi=data["obi"],
                depth_profile=data["depth_profile"],
            )
            print(f"saved npz: {out_path}")


if __name__ == "__main__":
    main()
