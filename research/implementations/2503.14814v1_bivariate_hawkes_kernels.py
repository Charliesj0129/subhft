#!/usr/bin/env python3
"""
Bivariate Hawkes simulation: exponential vs power-law kernels.

- Two event types (e.g., bid/ask arrivals)
- Discrete-time approximation with finite history window
- Compare kernels via summary statistics
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
    seed: int = 42
    window: int = 200
    mu0: float = 8.0
    mu1: float = 8.0
    alpha00: float = 0.3
    alpha01: float = 0.1
    alpha10: float = 0.1
    alpha11: float = 0.3
    max_rate: float = 200.0


@dataclass
class KernelConfig:
    kernel: str = "exp"
    beta: float = 1.5
    plaw_p: float = 1.4
    plaw_c: float = 0.05


def _kernel_weights(cfg: KernelConfig, window: int, dt: float) -> np.ndarray:
    lags = (np.arange(1, window + 1, dtype=np.float64)) * dt
    if cfg.kernel == "exp":
        return np.exp(-cfg.beta * lags)
    if cfg.kernel == "power-law":
        weights = np.power(lags + cfg.plaw_c, -cfg.plaw_p)
        # Normalize total influence to match the exponential kernel's L1 mass.
        ref = np.exp(-cfg.beta * lags)
        scale = ref.sum() / max(weights.sum(), 1e-12)
        return weights * scale
    raise ValueError(f"unknown kernel: {cfg.kernel}")


def _corr(x: np.ndarray, y: np.ndarray) -> float:
    if x.size < 2 or y.size < 2:
        return float("nan")
    if np.std(x) == 0.0 or np.std(y) == 0.0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def simulate(sim: SimConfig, kcfg: KernelConfig) -> Dict[str, np.ndarray]:
    rng = np.random.default_rng(sim.seed)
    alpha = np.array(
        [[sim.alpha00, sim.alpha01], [sim.alpha10, sim.alpha11]], dtype=np.float64
    )
    mu = np.array([sim.mu0, sim.mu1], dtype=np.float64)

    weights = _kernel_weights(kcfg, sim.window, sim.dt)
    history = np.zeros((sim.window, 2), dtype=np.float64)

    time = np.zeros(sim.steps, dtype=np.float64)
    intensities = np.zeros((sim.steps, 2), dtype=np.float64)
    counts = np.zeros((sim.steps, 2), dtype=np.int64)

    for step in range(sim.steps):
        conv = (weights[:, None] * history).sum(axis=0)
        rate = mu + alpha @ conv
        rate = np.clip(rate, 0.0, sim.max_rate)
        counts_step = rng.poisson(rate * sim.dt).astype(np.int64)

        time[step] = (step + 1) * sim.dt
        intensities[step] = rate
        counts[step] = counts_step

        history[1:] = history[:-1]
        history[0] = counts_step

    return {
        "time": time,
        "intensities": intensities,
        "counts": counts,
        "weights": weights,
    }


def summarize(data: Dict[str, np.ndarray]) -> Dict[str, float]:
    counts = data["counts"]
    intensities = data["intensities"]
    mean_counts = counts.mean(axis=0)
    mean_rate = intensities.mean(axis=0)
    corr = _corr(counts[:, 0], counts[:, 1])
    return {
        "mean_counts_0": float(mean_counts[0]),
        "mean_counts_1": float(mean_counts[1]),
        "mean_rate_0": float(mean_rate[0]),
        "mean_rate_1": float(mean_rate[1]),
        "corr_0_1": corr,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bivariate Hawkes: exponential vs power-law kernels"
    )
    parser.add_argument("--kernel", type=str, default="both", choices=["exp", "power-law", "both"])
    parser.add_argument("--steps", type=int, default=20000)
    parser.add_argument("--dt", type=float, default=0.01)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--window", type=int, default=200)
    parser.add_argument("--mu0", type=float, default=8.0)
    parser.add_argument("--mu1", type=float, default=8.0)
    parser.add_argument("--alpha00", type=float, default=0.3)
    parser.add_argument("--alpha01", type=float, default=0.1)
    parser.add_argument("--alpha10", type=float, default=0.1)
    parser.add_argument("--alpha11", type=float, default=0.3)
    parser.add_argument("--max-rate", type=float, default=200.0)
    parser.add_argument("--beta", type=float, default=1.5)
    parser.add_argument("--plaw-p", type=float, default=1.4)
    parser.add_argument("--plaw-c", type=float, default=0.05)
    parser.add_argument("--out-prefix", type=str, default="")
    return parser.parse_args()


def build_configs(args: argparse.Namespace) -> Tuple[SimConfig, KernelConfig, KernelConfig]:
    if args.steps <= 0:
        raise ValueError("steps must be > 0")
    if args.dt <= 0:
        raise ValueError("dt must be > 0")
    if args.window <= 0:
        raise ValueError("window must be > 0")
    if args.max_rate <= 0:
        raise ValueError("max_rate must be > 0")
    if args.plaw_p <= 0:
        raise ValueError("plaw_p must be > 0")
    if args.plaw_c <= 0:
        raise ValueError("plaw_c must be > 0")

    sim = SimConfig(
        steps=args.steps,
        dt=args.dt,
        seed=args.seed,
        window=args.window,
        mu0=args.mu0,
        mu1=args.mu1,
        alpha00=args.alpha00,
        alpha01=args.alpha01,
        alpha10=args.alpha10,
        alpha11=args.alpha11,
        max_rate=args.max_rate,
    )
    exp_cfg = KernelConfig(kernel="exp", beta=args.beta, plaw_p=args.plaw_p, plaw_c=args.plaw_c)
    pl_cfg = KernelConfig(kernel="power-law", beta=args.beta, plaw_p=args.plaw_p, plaw_c=args.plaw_c)
    return sim, exp_cfg, pl_cfg


def save_npz(path: str, data: Dict[str, np.ndarray]) -> None:
    np.savez(
        path,
        time=data["time"],
        intensities=data["intensities"],
        counts=data["counts"],
        weights=data["weights"],
    )


def main() -> None:
    args = parse_args()
    sim, exp_cfg, pl_cfg = build_configs(args)

    def run_and_report(label: str, kcfg: KernelConfig) -> None:
        data = simulate(sim, kcfg)
        stats = summarize(data)
        print(f"{label} kernel summary")
        print("mean_counts:", round(stats["mean_counts_0"], 4), round(stats["mean_counts_1"], 4))
        print("mean_rate:", round(stats["mean_rate_0"], 4), round(stats["mean_rate_1"], 4))
        print("corr_0_1:", round(stats["corr_0_1"], 4))
        if args.out_prefix:
            out_path = f"{args.out_prefix}_{label}.npz"
            save_npz(out_path, data)
            print(f"saved npz: {out_path}")

    if args.kernel in ("exp", "both"):
        run_and_report("exp", exp_cfg)
    if args.kernel in ("power-law", "both"):
        run_and_report("plaw", pl_cfg)


if __name__ == "__main__":
    main()
