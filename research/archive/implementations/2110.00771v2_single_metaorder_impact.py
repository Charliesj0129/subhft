#!/usr/bin/env python3
"""
Single-metaorder price impact measurement (non-average).

- Simulate a baseline LOB price path
- Inject a single metaorder (sell) into the flow
- Measure impact as price difference vs. control
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
    # Metaorder
    meta_start: int = 300
    meta_duration: int = 200
    meta_size: int = 500


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


def simulate(cfg: SimConfig, inject_meta: bool) -> Dict[str, np.ndarray]:
    rng = np.random.default_rng(cfg.seed)
    bid = np.full(cfg.levels, cfg.base_depth, dtype=np.int64)
    ask = np.full(cfg.levels, cfg.base_depth, dtype=np.int64)

    samples = cfg.steps // cfg.sample_every + 1
    times = np.zeros(samples, dtype=np.float64)
    mid = np.zeros(samples, dtype=np.int64)

    best_bid = _best_price(bid, "bid", cfg.mid_price, cfg.tick_size, cfg.levels)
    best_ask = _best_price(ask, "ask", cfg.mid_price, cfg.tick_size, cfg.levels)
    mid[0] = (best_bid + best_ask) // 2

    s_idx = 1
    meta_remaining = cfg.meta_size
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

        if inject_meta and cfg.meta_start <= step < cfg.meta_start + cfg.meta_duration and meta_remaining > 0:
            slice_size = int(np.ceil(cfg.meta_size / cfg.meta_duration))
            exec_size = min(slice_size, meta_remaining)
            _consume_from_best(bid, exec_size, cfg.levels)
            meta_remaining -= exec_size

        if bid.sum() == 0:
            bid[-1] = cfg.base_depth
        if ask.sum() == 0:
            ask[-1] = cfg.base_depth

        if step % cfg.sample_every == 0:
            times[s_idx] = step * cfg.dt
            best_bid = _best_price(bid, "bid", cfg.mid_price, cfg.tick_size, cfg.levels)
            best_ask = _best_price(ask, "ask", cfg.mid_price, cfg.tick_size, cfg.levels)
            mid[s_idx] = (best_bid + best_ask) // 2
            s_idx += 1

    return {"time": times, "mid": mid}


def measure_impact(control: np.ndarray, treated: np.ndarray) -> Dict[str, float]:
    diff = treated - control
    return {
        "impact_mean": float(diff.mean()),
        "impact_p95": float(np.percentile(diff, 95)),
        "impact_min": float(diff.min()),
        "impact_max": float(diff.max()),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Single metaorder impact measurement")
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
    parser.add_argument("--meta-start", type=int, default=300)
    parser.add_argument("--meta-duration", type=int, default=200)
    parser.add_argument("--meta-size", type=int, default=500)
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
    if args.meta_duration <= 0:
        raise ValueError("meta_duration must be > 0")
    if args.meta_size <= 0:
        raise ValueError("meta_size must be > 0")

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
        meta_start=args.meta_start,
        meta_duration=args.meta_duration,
        meta_size=args.meta_size,
    )


def main() -> None:
    args = parse_args()
    cfg = build_config(args)

    control = simulate(cfg, inject_meta=False)
    treated = simulate(cfg, inject_meta=True)
    stats = measure_impact(control["mid"], treated["mid"])

    print("Single metaorder impact summary")
    print("impact_mean:", round(stats["impact_mean"], 4))
    print("impact_p95:", round(stats["impact_p95"], 4))
    print("impact_min:", round(stats["impact_min"], 4))
    print("impact_max:", round(stats["impact_max"], 4))

    if args.out:
        np.savez(
            args.out,
            time=control["time"],
            control_mid=control["mid"],
            treated_mid=treated["mid"],
            impact=treated["mid"] - control["mid"],
        )
        print(f"saved npz: {args.out}")


if __name__ == "__main__":
    main()
