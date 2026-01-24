#!/usr/bin/env python3
"""
Event-based LOB simulation with a neural-Hawkes-style intensity module.

- Discrete tick grid around a fixed reference mid
- Event types: limit/cancel/market on bid/ask
- Neural intensity depends on decayed event history + book features
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np


EVENT_TYPES = [
    "limit_bid",
    "limit_ask",
    "cancel_bid",
    "cancel_ask",
    "market_buy",
    "market_sell",
]


def softplus(x: np.ndarray) -> np.ndarray:
    return np.log1p(np.exp(-np.abs(x))) + np.maximum(x, 0.0)


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
    size_dist: str = "poisson"
    size_param1: float = 8.0
    size_param2: float = 0.0
    depth_geo_p: float = 0.35


@dataclass
class NeuralConfig:
    decay: float = 1.2
    hidden_size: int = 12
    base_rate: float = 5.0
    weight_scale: float = 0.1
    max_rate: float = 200.0


class NeuralHawkes:
    def __init__(self, event_dim: int, feature_dim: int, cfg: NeuralConfig, seed: int) -> None:
        self.event_dim = event_dim
        self.feature_dim = feature_dim
        self.cfg = cfg
        self.rng = np.random.default_rng(seed)

        self.memory = np.zeros(event_dim, dtype=np.float64)
        self.w1 = self.rng.normal(scale=cfg.weight_scale, size=(cfg.hidden_size, event_dim + feature_dim))
        self.b1 = self.rng.normal(scale=cfg.weight_scale, size=(cfg.hidden_size,))
        self.w2 = self.rng.normal(scale=cfg.weight_scale, size=(event_dim, cfg.hidden_size))
        self.b2 = self.rng.normal(scale=cfg.weight_scale, size=(event_dim,))

    def decay(self, dt: float) -> None:
        decay = np.exp(-self.cfg.decay * dt)
        self.memory *= decay

    def excite(self, counts: np.ndarray) -> None:
        self.memory += counts

    def intensity(self, features: np.ndarray) -> np.ndarray:
        base = np.full(self.event_dim, self.cfg.base_rate, dtype=np.float64)
        x = np.concatenate([self.memory, features])
        hidden = np.tanh(self.w1 @ x + self.b1)
        logits = self.w2 @ hidden + self.b2 + base
        rate = softplus(logits)
        return np.clip(rate, 0.0, self.cfg.max_rate)


class NeuralHawkesLOB:
    def __init__(self, cfg: SimConfig, ncfg: NeuralConfig) -> None:
        self.cfg = cfg
        self.ncfg = ncfg
        self.rng = np.random.default_rng(cfg.seed)
        self.bid = np.full(cfg.levels, cfg.base_depth, dtype=np.int64)
        self.ask = np.full(cfg.levels, cfg.base_depth, dtype=np.int64)
        self.neural = NeuralHawkes(len(EVENT_TYPES), self._feature_dim(), ncfg, cfg.seed + 7)
        self.event_log: List[Tuple[int, str, int, int]] = []

    def _feature_dim(self) -> int:
        return 4

    def _sample_size(self) -> int:
        cfg = self.cfg
        if cfg.size_dist == "poisson":
            size = int(self.rng.poisson(cfg.size_param1)) + 1
        elif cfg.size_dist == "geometric":
            size = int(self.rng.geometric(cfg.size_param1))
        elif cfg.size_dist == "lognormal":
            size = int(self.rng.lognormal(cfg.size_param1, cfg.size_param2))
        else:
            raise ValueError(f"unknown size_dist: {cfg.size_dist}")
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

    def _consume_from_best(self, depth: np.ndarray, size: int) -> int:
        remaining = size
        for level in range(self.cfg.levels):
            if remaining <= 0:
                break
            available = int(depth[level])
            if available <= 0:
                continue
            take = min(available, remaining)
            depth[level] -= take
            remaining -= take
        return size - remaining

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

    def _features(self) -> np.ndarray:
        cfg = self.cfg
        best_bid = self._best_price("bid")
        best_ask = self._best_price("ask")
        spread_ticks = (best_ask - best_bid) // cfg.tick_size
        total_bid = int(self.bid.sum())
        total_ask = int(self.ask.sum())
        total = float(total_bid + total_ask)
        obi = (total_bid - total_ask) / total if total > 0 else 0.0

        best_bid_depth = float(self.bid[0])
        best_ask_depth = float(self.ask[0])
        best_total = best_bid_depth + best_ask_depth
        best_obi = (best_bid_depth - best_ask_depth) / best_total if best_total > 0 else 0.0

        depth_scale = cfg.base_depth * cfg.levels
        depth_ratio = total / float(2 * depth_scale) if depth_scale > 0 else 0.0

        return np.array(
            [spread_ticks / max(1, cfg.levels), obi, best_obi, depth_ratio], dtype=np.float64
        )

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

    def _step(self, step: int) -> Tuple[np.ndarray, np.ndarray]:
        cfg = self.cfg
        self.neural.decay(cfg.dt)
        features = self._features()
        intensity = self.neural.intensity(features)

        counts = self.rng.poisson(intensity * cfg.dt).astype(np.int64)
        actual = np.zeros_like(counts)

        # Limit orders
        for _ in range(int(counts[0])):
            level = self._sample_level()
            size = self._sample_size()
            self.bid[level] += size
            self._record_event(step, EVENT_TYPES[0], level, size)
            actual[0] += 1
        for _ in range(int(counts[1])):
            level = self._sample_level()
            size = self._sample_size()
            self.ask[level] += size
            self._record_event(step, EVENT_TYPES[1], level, size)
            actual[1] += 1

        # Cancellations
        for _ in range(int(counts[2])):
            level = self._choose_cancel_level(self.bid)
            size = self._sample_size()
            cancel = min(size, int(self.bid[level]))
            if cancel > 0:
                self.bid[level] -= cancel
                self._record_event(step, EVENT_TYPES[2], level, cancel)
                actual[2] += 1
        for _ in range(int(counts[3])):
            level = self._choose_cancel_level(self.ask)
            size = self._sample_size()
            cancel = min(size, int(self.ask[level]))
            if cancel > 0:
                self.ask[level] -= cancel
                self._record_event(step, EVENT_TYPES[3], level, cancel)
                actual[3] += 1

        # Market orders
        for _ in range(int(counts[4])):
            self._ensure_liquidity()
            size = self._sample_size()
            filled = self._consume_from_best(self.ask, size)
            if filled > 0:
                self._record_event(step, EVENT_TYPES[4], 0, filled)
                actual[4] += 1
        for _ in range(int(counts[5])):
            self._ensure_liquidity()
            size = self._sample_size()
            filled = self._consume_from_best(self.bid, size)
            if filled > 0:
                self._record_event(step, EVENT_TYPES[5], 0, filled)
                actual[5] += 1

        self._ensure_liquidity()
        self.neural.excite(actual.astype(np.float64))

        return intensity, actual

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
        intensities = np.zeros((samples, len(EVENT_TYPES)), dtype=np.float64)

        bid_depth[0] = self.bid
        ask_depth[0] = self.ask
        best_bid[0] = self._best_price("bid")
        best_ask[0] = self._best_price("ask")
        spread[0] = best_ask[0] - best_bid[0]
        mid_x2[0] = best_bid[0] + best_ask[0]
        total0 = float(self.bid.sum() + self.ask.sum())
        obi[0] = (self.bid.sum() - self.ask.sum()) / total0 if total0 > 0 else 0.0
        intensities[0] = self.neural.intensity(self._features())

        s_idx = 1

        for step in range(1, cfg.steps + 1):
            intensity, _ = self._step(step)
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
                intensities[s_idx] = intensity
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
            "intensities": intensities,
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
    parser = argparse.ArgumentParser(
        description="Event-based LOB with neural-Hawkes-style intensities"
    )
    parser.add_argument("--levels", type=int, default=10)
    parser.add_argument("--steps", type=int, default=20000)
    parser.add_argument("--dt", type=float, default=0.01)
    parser.add_argument("--tick-size", type=int, default=1)
    parser.add_argument("--mid-price", type=int, default=1000)
    parser.add_argument("--base-depth", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sample-every", type=int, default=20)
    parser.add_argument("--max-events", type=int, default=20000)
    parser.add_argument("--size-dist", type=str, default="poisson")
    parser.add_argument("--size-param1", type=float, default=8.0)
    parser.add_argument("--size-param2", type=float, default=0.0)
    parser.add_argument("--depth-geo-p", type=float, default=0.35)
    parser.add_argument("--decay", type=float, default=1.2)
    parser.add_argument("--hidden-size", type=int, default=12)
    parser.add_argument("--base-rate", type=float, default=5.0)
    parser.add_argument("--weight-scale", type=float, default=0.1)
    parser.add_argument("--max-rate", type=float, default=200.0)
    parser.add_argument("--out", type=str, default="")
    parser.add_argument("--events", type=str, default="")
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> Tuple[SimConfig, NeuralConfig]:
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

    cfg = SimConfig(
        levels=args.levels,
        steps=args.steps,
        dt=args.dt,
        tick_size=args.tick_size,
        mid_price=args.mid_price,
        base_depth=args.base_depth,
        seed=args.seed,
        sample_every=args.sample_every,
        max_events=args.max_events,
        size_dist=args.size_dist,
        size_param1=args.size_param1,
        size_param2=args.size_param2,
        depth_geo_p=args.depth_geo_p,
    )
    ncfg = NeuralConfig(
        decay=args.decay,
        hidden_size=args.hidden_size,
        base_rate=args.base_rate,
        weight_scale=args.weight_scale,
        max_rate=args.max_rate,
    )
    return cfg, ncfg


def save_events(path: str, events: List[Tuple[int, str, int, int]]) -> None:
    if not events:
        return
    header = "step,event,level,size"
    arr = np.array(events, dtype=object)
    np.savetxt(path, arr, fmt="%s", delimiter=",", header=header)


def main() -> None:
    args = parse_args()
    cfg, ncfg = build_config(args)

    sim = NeuralHawkesLOB(cfg, ncfg)
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
            intensities=data["intensities"],
            event_types=np.array(EVENT_TYPES),
        )
        print(f"saved npz: {args.out}")

    if args.events:
        save_events(args.events, sim.event_log)
        print(f"saved events: {args.events}")


if __name__ == "__main__":
    main()
