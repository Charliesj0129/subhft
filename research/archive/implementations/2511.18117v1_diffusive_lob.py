#!/usr/bin/env python3
"""
Diffusive-limit LOB prototype (Hawkes-driven + liquidity migration).

- Bid-side queues on discrete price levels (ticks)
- Self-exciting Hawkes intensities (exponential kernel)
- Liquidity migrates to adjacent levels
- Diffusive scaling via queue size normalization (scale_n)
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Dict, Tuple

import numpy as np


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
    levels: int = 5
    steps: int = 20000
    dt: float = 0.01
    scale_n: int = 100
    base_size: int = 5
    seed: int = 42
    sample_every: int = 10
    # Hawkes params (shared across levels)
    mu_arrival: float = 6.0
    alpha_arrival: float = 0.4
    beta_arrival: float = 1.4
    mu_cancel: float = 5.0
    alpha_cancel: float = 0.3
    beta_cancel: float = 1.2
    mu_migrate: float = 2.5
    alpha_migrate: float = 0.2
    beta_migrate: float = 1.0


class DiffusiveLOB:
    """Bid-side queue model with Hawkes-driven events and migration."""

    def __init__(self, cfg: SimConfig) -> None:
        self.cfg = cfg
        self.rng = np.random.default_rng(cfg.seed)
        self.queue = np.full(cfg.levels, cfg.base_size * cfg.scale_n, dtype=np.int64)

        self.arrival = [
            HawkesExp(cfg.mu_arrival, cfg.alpha_arrival, cfg.beta_arrival)
            for _ in range(cfg.levels)
        ]
        self.cancel = [
            HawkesExp(cfg.mu_cancel, cfg.alpha_cancel, cfg.beta_cancel)
            for _ in range(cfg.levels)
        ]
        self.migrate_up = [
            HawkesExp(cfg.mu_migrate, cfg.alpha_migrate, cfg.beta_migrate)
            for _ in range(cfg.levels)
        ]
        self.migrate_down = [
            HawkesExp(cfg.mu_migrate, cfg.alpha_migrate, cfg.beta_migrate)
            for _ in range(cfg.levels)
        ]

    def _step(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        cfg = self.cfg
        arrivals = np.zeros(cfg.levels, dtype=np.int64)
        cancels = np.zeros(cfg.levels, dtype=np.int64)
        mig_up = np.zeros(cfg.levels, dtype=np.int64)
        mig_down = np.zeros(cfg.levels, dtype=np.int64)

        for i in range(cfg.levels):
            self.arrival[i].decay(cfg.dt)
            self.cancel[i].decay(cfg.dt)
            self.migrate_up[i].decay(cfg.dt)
            self.migrate_down[i].decay(cfg.dt)

            arrivals[i] = self.arrival[i].sample(cfg.dt, self.rng)
            cancels[i] = self.cancel[i].sample(cfg.dt, self.rng)
            mig_up[i] = self.migrate_up[i].sample(cfg.dt, self.rng)
            mig_down[i] = self.migrate_down[i].sample(cfg.dt, self.rng)

        # Cancel first, then migrate, then arrivals
        cancels = np.minimum(cancels, self.queue)
        self.queue -= cancels

        # Migration toward deeper levels (i -> i+1)
        for i in range(cfg.levels - 1):
            move = min(mig_up[i], int(self.queue[i]))
            if move > 0:
                self.queue[i] -= move
                self.queue[i + 1] += move
            mig_up[i] = move
        mig_up[-1] = 0

        # Migration toward best (i -> i-1)
        for i in range(1, cfg.levels):
            move = min(mig_down[i], int(self.queue[i]))
            if move > 0:
                self.queue[i] -= move
                self.queue[i - 1] += move
            mig_down[i] = move
        mig_down[0] = 0

        # Arrivals add new queue depth
        self.queue += arrivals

        # Hawkes self-excitation updates
        for i in range(cfg.levels):
            self.arrival[i].excite(arrivals[i])
            self.cancel[i].excite(cancels[i])
            self.migrate_up[i].excite(mig_up[i])
            self.migrate_down[i].excite(mig_down[i])

        return arrivals, cancels, mig_up, mig_down

    def run(self) -> Dict[str, np.ndarray]:
        cfg = self.cfg
        samples = cfg.steps // cfg.sample_every + 1
        times = np.zeros(samples, dtype=np.float64)
        queue_scaled = np.zeros((samples, cfg.levels), dtype=np.float64)

        queue_scaled[0] = self.queue / cfg.scale_n
        s_idx = 1

        for step in range(1, cfg.steps + 1):
            self._step()
            if step % cfg.sample_every == 0:
                times[s_idx] = step * cfg.dt
                queue_scaled[s_idx] = self.queue / cfg.scale_n
                s_idx += 1

        return {
            "time": times,
            "queue_scaled": queue_scaled,
        }


def autocorr_lag1(series: np.ndarray) -> float:
    if series.size < 2:
        return float("nan")
    x = series[:-1]
    y = series[1:]
    if np.std(x) == 0.0 or np.std(y) == 0.0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def summarize(queue_scaled: np.ndarray) -> Dict[str, np.ndarray]:
    mean = queue_scaled.mean(axis=0)
    var = queue_scaled.var(axis=0)
    ac1 = autocorr_lag1(queue_scaled[:, 0])
    return {
        "mean": mean,
        "var": var,
        "ac1_best_level": ac1,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Diffusive-limit LOB prototype (Hawkes + liquidity migration)"
    )
    parser.add_argument("--levels", type=int, default=5)
    parser.add_argument("--steps", type=int, default=20000)
    parser.add_argument("--dt", type=float, default=0.01)
    parser.add_argument("--scale-n", type=int, default=100)
    parser.add_argument("--base-size", type=int, default=5)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--sample-every", type=int, default=10)
    parser.add_argument("--out", type=str, default="")
    parser.add_argument("--csv", type=str, default="")
    parser.add_argument("--mu-arrival", type=float, default=6.0)
    parser.add_argument("--alpha-arrival", type=float, default=0.4)
    parser.add_argument("--beta-arrival", type=float, default=1.4)
    parser.add_argument("--mu-cancel", type=float, default=5.0)
    parser.add_argument("--alpha-cancel", type=float, default=0.3)
    parser.add_argument("--beta-cancel", type=float, default=1.2)
    parser.add_argument("--mu-migrate", type=float, default=2.5)
    parser.add_argument("--alpha-migrate", type=float, default=0.2)
    parser.add_argument("--beta-migrate", type=float, default=1.0)
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> SimConfig:
    if args.levels < 2:
        raise ValueError("levels must be >= 2")
    if args.steps <= 0:
        raise ValueError("steps must be > 0")
    if args.dt <= 0:
        raise ValueError("dt must be > 0")
    if args.scale_n <= 0:
        raise ValueError("scale_n must be > 0")
    if args.sample_every <= 0:
        raise ValueError("sample_every must be > 0")

    return SimConfig(
        levels=args.levels,
        steps=args.steps,
        dt=args.dt,
        scale_n=args.scale_n,
        base_size=args.base_size,
        seed=args.seed,
        sample_every=args.sample_every,
        mu_arrival=args.mu_arrival,
        alpha_arrival=args.alpha_arrival,
        beta_arrival=args.beta_arrival,
        mu_cancel=args.mu_cancel,
        alpha_cancel=args.alpha_cancel,
        beta_cancel=args.beta_cancel,
        mu_migrate=args.mu_migrate,
        alpha_migrate=args.alpha_migrate,
        beta_migrate=args.beta_migrate,
    )


def main() -> None:
    args = parse_args()
    cfg = build_config(args)

    sim = DiffusiveLOB(cfg)
    data = sim.run()
    stats = summarize(data["queue_scaled"])

    print("Simulation summary")
    print(f"levels={cfg.levels} steps={cfg.steps} dt={cfg.dt} scale_n={cfg.scale_n}")
    print("mean(queue_scaled) per level:", np.round(stats["mean"], 4))
    print("var(queue_scaled) per level:", np.round(stats["var"], 4))
    print("ac1(best level):", round(stats["ac1_best_level"], 4))

    if args.out:
        np.savez(
            args.out,
            time=data["time"],
            queue_scaled=data["queue_scaled"],
        )
        print(f"saved npz: {args.out}")

    if args.csv:
        header = ",".join([f"lvl{i}" for i in range(cfg.levels)])
        np.savetxt(args.csv, data["queue_scaled"], delimiter=",", header=header)
        print(f"saved csv: {args.csv}")


if __name__ == "__main__":
    main()
