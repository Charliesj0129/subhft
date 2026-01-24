#!/usr/bin/env python3
"""
Compound Hawkes LOB prototype.

- Hawkes intensity drives event arrivals
- Each event has a random size (mark)
- Sizes sampled from parametric distributions
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np


EVENT_TYPES = ["limit_bid", "limit_ask", "cancel_bid", "cancel_ask"]


@dataclass
class HawkesExp:
    mu: float
    alpha: float
    beta: float
    intensity: float = None

    def __post_init__(self) -> None:
        self.intensity = float(self.mu)

    def decay(self, dt: float) -> None:
        decay = np.exp(-self.beta * dt)
        self.intensity = self.mu + (self.intensity - self.mu) * decay

    def excite(self, count: int) -> None:
        if count > 0:
            self.intensity += self.alpha * count

    def sample(self, dt: float, rng: np.random.Generator) -> int:
        rate = max(self.intensity, 0.0)
        return int(rng.poisson(rate * dt))


@dataclass
class SimConfig:
    levels: int = 10
    steps: int = 20000
    dt: float = 0.01
    tick_size: int = 1
    mid_price: int = 1000
    base_depth: int = 50
    seed: int = 42
    sample_every: int = 20
    max_events: int = 20000
    depth_geo_p: float = 0.35
    # Hawkes params
    mu_limit: float = 10.0
    alpha_limit: float = 0.35
    beta_limit: float = 1.5
    mu_cancel: float = 8.0
    alpha_cancel: float = 0.3
    beta_cancel: float = 1.2
    # Mark distributions
    mark_dist: str = "lognormal"
    mark_param1: float = 1.5
    mark_param2: float = 0.5


class CompoundHawkesLOB:
    def __init__(self, cfg: SimConfig) -> None:
        self.cfg = cfg
        self.rng = np.random.default_rng(cfg.seed)
        self.bid = np.full(cfg.levels, cfg.base_depth, dtype=np.int64)
        self.ask = np.full(cfg.levels, cfg.base_depth, dtype=np.int64)
        self.processes = {
            "limit_bid": HawkesExp(cfg.mu_limit, cfg.alpha_limit, cfg.beta_limit),
            "limit_ask": HawkesExp(cfg.mu_limit, cfg.alpha_limit, cfg.beta_limit),
            "cancel_bid": HawkesExp(cfg.mu_cancel, cfg.alpha_cancel, cfg.beta_cancel),
            "cancel_ask": HawkesExp(cfg.mu_cancel, cfg.alpha_cancel, cfg.beta_cancel),
        }
        self.event_log: List[Tuple[int, str, int, int]] = []

    def _sample_mark(self) -> int:
        cfg = self.cfg
        if cfg.mark_dist == "poisson":
            size = int(self.rng.poisson(cfg.mark_param1)) + 1
        elif cfg.mark_dist == "geometric":
            size = int(self.rng.geometric(cfg.mark_param1))
        elif cfg.mark_dist == "lognormal":
            size = int(self.rng.lognormal(cfg.mark_param1, cfg.mark_param2))
        else:
            raise ValueError(f"unknown mark_dist: {cfg.mark_dist}")
        return max(1, size)

    def _sample_level(self) -> int:
        cfg = self.cfg
        level = int(self.rng.geometric(cfg.depth_geo_p)) - 1
        return min(max(level, 0), cfg.levels - 1)

    def _choose_cancel_level(self, depth: np.ndarray) -> int:
        total = int(depth.sum())
        if total <= 0:
            return 0
        weights = depth / total
        return int(self.rng.choice(len(depth), p=weights))

    def _record_event(self, step: int, event: str, level: int, size: int) -> None:
        if len(self.event_log) >= self.cfg.max_events:
            return
        self.event_log.append((step, event, level, size))

    def _ensure_liquidity(self) -> None:
        cfg = self.cfg
        if self.bid.sum() == 0:
            self.bid[-1] = cfg.base_depth
        if self.ask.sum() == 0:
            self.ask[-1] = cfg.base_depth

    def _best_price(self, side: str) -> int:
        cfg = self.cfg
        depth = self.bid if side == "bid" else self.ask
        levels = np.where(depth > 0)[0]
        if levels.size == 0:
            level = cfg.levels - 1
        else:
            level = int(levels[0])
        if side == "bid":
            return cfg.mid_price - cfg.tick_size * (1 + level)
        return cfg.mid_price + cfg.tick_size * (1 + level)

    def _step(self, step: int) -> Dict[str, int]:
        cfg = self.cfg
        counts: Dict[str, int] = {}
        for name, proc in self.processes.items():
            proc.decay(cfg.dt)
            counts[name] = proc.sample(cfg.dt, self.rng)

        for _ in range(counts["limit_bid"]):
            level = self._sample_level()
            size = self._sample_mark()
            self.bid[level] += size
            self._record_event(step, "limit_bid", level, size)
        for _ in range(counts["limit_ask"]):
            level = self._sample_level()
            size = self._sample_mark()
            self.ask[level] += size
            self._record_event(step, "limit_ask", level, size)

        for _ in range(counts["cancel_bid"]):
            level = self._choose_cancel_level(self.bid)
            size = self._sample_mark()
            cancel = min(size, int(self.bid[level]))
            if cancel > 0:
                self.bid[level] -= cancel
                self._record_event(step, "cancel_bid", level, cancel)
        for _ in range(counts["cancel_ask"]):
            level = self._choose_cancel_level(self.ask)
            size = self._sample_mark()
            cancel = min(size, int(self.ask[level]))
            if cancel > 0:
                self.ask[level] -= cancel
                self._record_event(step, "cancel_ask", level, cancel)

        self._ensure_liquidity()

        for name, proc in self.processes.items():
            proc.excite(counts[name])

        return counts

    def run(self) -> Dict[str, np.ndarray]:
        cfg = self.cfg
        samples = cfg.steps // cfg.sample_every + 1
        times = np.zeros(samples, dtype=np.float64)
        bid_depth = np.zeros((samples, cfg.levels), dtype=np.int64)
        ask_depth = np.zeros((samples, cfg.levels), dtype=np.int64)
        best_bid = np.zeros(samples, dtype=np.int64)
        best_ask = np.zeros(samples, dtype=np.int64)
        spread = np.zeros(samples, dtype=np.int64)
        mid_x2 = np.zeros(samples, dtype=np.int64)
        obi = np.zeros(samples, dtype=np.float64)

        bid_depth[0] = self.bid
        ask_depth[0] = self.ask
        best_bid[0] = self._best_price("bid")
        best_ask[0] = self._best_price("ask")
        spread[0] = best_ask[0] - best_bid[0]
        mid_x2[0] = best_bid[0] + best_ask[0]
        total0 = float(self.bid.sum() + self.ask.sum())
        obi[0] = (self.bid.sum() - self.ask.sum()) / total0 if total0 > 0 else 0.0

        s_idx = 1

        for step in range(1, cfg.steps + 1):
            self._step(step)
            if step % cfg.sample_every == 0:
                times[s_idx] = step * cfg.dt
                bid_depth[s_idx] = self.bid
                ask_depth[s_idx] = self.ask
                best_bid[s_idx] = self._best_price("bid")
                best_ask[s_idx] = self._best_price("ask")
                spread[s_idx] = best_ask[s_idx] - best_bid[s_idx]
                mid_x2[s_idx] = best_bid[s_idx] + best_ask[s_idx]
                total = float(self.bid.sum() + self.ask.sum())
                obi[s_idx] = (self.bid.sum() - self.ask.sum()) / total if total > 0 else 0.0
                s_idx += 1

        return {
            "time": times,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "mid_x2": mid_x2,
            "obi": obi,
        }


def summarize(data: Dict[str, np.ndarray]) -> Dict[str, float]:
    spread = data["spread"]
    obi = data["obi"]
    return {
        "spread_mean": float(spread.mean()),
        "spread_p95": float(np.percentile(spread, 95)),
        "obi_mean": float(obi.mean()),
        "obi_std": float(obi.std()),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compound Hawkes LOB prototype")
    parser.add_argument("--levels", type=int, default=10)
    parser.add_argument("--steps", type=int, default=20000)
    parser.add_argument("--dt", type=float, default=0.01)
    parser.add_argument("--tick-size", type=int, default=1)
    parser.add_argument("--mid-price", type=int, default=1000)
    parser.add_argument("--base-depth", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sample-every", type=int, default=20)
    parser.add_argument("--max-events", type=int, default=20000)
    parser.add_argument("--depth-geo-p", type=float, default=0.35)
    parser.add_argument("--mu-limit", type=float, default=10.0)
    parser.add_argument("--alpha-limit", type=float, default=0.35)
    parser.add_argument("--beta-limit", type=float, default=1.5)
    parser.add_argument("--mu-cancel", type=float, default=8.0)
    parser.add_argument("--alpha-cancel", type=float, default=0.3)
    parser.add_argument("--beta-cancel", type=float, default=1.2)
    parser.add_argument("--mark-dist", type=str, default="lognormal")
    parser.add_argument("--mark-param1", type=float, default=1.5)
    parser.add_argument("--mark-param2", type=float, default=0.5)
    parser.add_argument("--out", type=str, default="")
    parser.add_argument("--events", type=str, default="")
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> SimConfig:
    if args.levels < 2:
        raise ValueError("levels must be >= 2")
    if args.steps <= 0:
        raise ValueError("steps must be > 0")
    if args.dt <= 0:
        raise ValueError("dt must be > 0")
    if args.tick_size <= 0:
        raise ValueError("tick_size must be > 0")
    if args.sample_every <= 0:
        raise ValueError("sample_every must be > 0")
    if args.depth_geo_p <= 0.0 or args.depth_geo_p >= 1.0:
        raise ValueError("depth_geo_p must be in (0, 1)")

    return SimConfig(
        levels=args.levels,
        steps=args.steps,
        dt=args.dt,
        tick_size=args.tick_size,
        mid_price=args.mid_price,
        base_depth=args.base_depth,
        seed=args.seed,
        sample_every=args.sample_every,
        max_events=args.max_events,
        depth_geo_p=args.depth_geo_p,
        mu_limit=args.mu_limit,
        alpha_limit=args.alpha_limit,
        beta_limit=args.beta_limit,
        mu_cancel=args.mu_cancel,
        alpha_cancel=args.alpha_cancel,
        beta_cancel=args.beta_cancel,
        mark_dist=args.mark_dist,
        mark_param1=args.mark_param1,
        mark_param2=args.mark_param2,
    )


def save_events(path: str, events: List[Tuple[int, str, int, int]]) -> None:
    if not events:
        return
    header = "step,event,level,size"
    arr = np.array(events, dtype=object)
    np.savetxt(path, arr, fmt="%s", delimiter=",", header=header)


def main() -> None:
    args = parse_args()
    cfg = build_config(args)

    sim = CompoundHawkesLOB(cfg)
    data = sim.run()
    stats = summarize(data)

    print("Simulation summary")
    print(
        f"levels={cfg.levels} steps={cfg.steps} dt={cfg.dt} tick={cfg.tick_size} "
        f"mid={cfg.mid_price}"
    )
    print("spread_mean:", round(stats["spread_mean"], 4))
    print("spread_p95:", round(stats["spread_p95"], 4))
    print("obi_mean:", round(stats["obi_mean"], 6))
    print("obi_std:", round(stats["obi_std"], 6))

    if args.out:
        np.savez(
            args.out,
            time=data["time"],
            bid_depth=data["bid_depth"],
            ask_depth=data["ask_depth"],
            best_bid=data["best_bid"],
            best_ask=data["best_ask"],
            spread=data["spread"],
            mid_x2=data["mid_x2"],
            obi=data["obi"],
        )
        print(f"saved npz: {args.out}")

    if args.events:
        save_events(args.events, sim.event_log)
        print(f"saved events: {args.events}")


if __name__ == "__main__":
    main()
