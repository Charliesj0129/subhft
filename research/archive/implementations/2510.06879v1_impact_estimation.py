#!/usr/bin/env python3
"""
Nonparametric self/cross-impact kernel estimation (simplified).

- Generate synthetic trade signs and returns for 2 assets
- Estimate impact kernels via linear regression on lagged order flow
- Bootstrap confidence bands
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Dict, Tuple

import numpy as np


@dataclass
class SimConfig:
    steps: int = 5000
    seed: int = 42
    lags: int = 20
    impact_scale: float = 0.02
    cross_scale: float = 0.01
    noise_std: float = 0.05
    bootstrap: int = 100


def simulate(cfg: SimConfig) -> Dict[str, np.ndarray]:
    rng = np.random.default_rng(cfg.seed)
    # trade signs (+1/-1) and sizes (1)
    q0 = rng.choice([-1.0, 1.0], size=cfg.steps)
    q1 = rng.choice([-1.0, 1.0], size=cfg.steps)

    # true kernel: decaying sqrt-like (discrete)
    lags = np.arange(1, cfg.lags + 1, dtype=np.float64)
    k_self = cfg.impact_scale / np.sqrt(lags)
    k_cross = cfg.cross_scale / np.sqrt(lags)

    r0 = np.zeros(cfg.steps, dtype=np.float64)
    r1 = np.zeros(cfg.steps, dtype=np.float64)

    for t in range(cfg.steps):
        acc0 = 0.0
        acc1 = 0.0
        for h in range(1, cfg.lags + 1):
            if t - h < 0:
                break
            acc0 += k_self[h - 1] * q0[t - h] + k_cross[h - 1] * q1[t - h]
            acc1 += k_self[h - 1] * q1[t - h] + k_cross[h - 1] * q0[t - h]
        r0[t] = acc0 + rng.normal(0.0, cfg.noise_std)
        r1[t] = acc1 + rng.normal(0.0, cfg.noise_std)

    return {"q0": q0, "q1": q1, "r0": r0, "r1": r1}


def build_design(q0: np.ndarray, q1: np.ndarray, lags: int) -> np.ndarray:
    rows = q0.size - lags
    X = np.zeros((rows, 2 * lags), dtype=np.float64)
    for i in range(rows):
        t = i + lags
        X[i, :lags] = q0[t - lags : t][::-1]
        X[i, lags:] = q1[t - lags : t][::-1]
    return X


def estimate_kernel(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    coef = np.linalg.lstsq(X, y, rcond=None)[0]
    return coef


def bootstrap_kernels(X: np.ndarray, y: np.ndarray, lags: int, boot: int, rng: np.random.Generator) -> np.ndarray:
    n = X.shape[0]
    boots = np.zeros((boot, 2 * lags), dtype=np.float64)
    for b in range(boot):
        idx = rng.integers(0, n, size=n)
        boots[b] = estimate_kernel(X[idx], y[idx])
    return boots


def summarize(coef: np.ndarray, boot: np.ndarray, lags: int) -> Dict[str, np.ndarray]:
    lower = np.percentile(boot, 5, axis=0)
    upper = np.percentile(boot, 95, axis=0)
    return {
        "self": coef[:lags],
        "cross": coef[lags:],
        "self_lo": lower[:lags],
        "self_hi": upper[:lags],
        "cross_lo": lower[lags:],
        "cross_hi": upper[lags:],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Nonparametric impact estimation")
    parser.add_argument("--steps", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--lags", type=int, default=20)
    parser.add_argument("--impact-scale", type=float, default=0.02)
    parser.add_argument("--cross-scale", type=float, default=0.01)
    parser.add_argument("--noise-std", type=float, default=0.05)
    parser.add_argument("--bootstrap", type=int, default=100)
    parser.add_argument("--out", type=str, default="")
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> SimConfig:
    if args.steps <= 0:
        raise ValueError("steps must be > 0")
    if args.lags <= 0:
        raise ValueError("lags must be > 0")
    if args.bootstrap < 0:
        raise ValueError("bootstrap must be >= 0")
    return SimConfig(
        steps=args.steps,
        seed=args.seed,
        lags=args.lags,
        impact_scale=args.impact_scale,
        cross_scale=args.cross_scale,
        noise_std=args.noise_std,
        bootstrap=args.bootstrap,
    )


def main() -> None:
    args = parse_args()
    cfg = build_config(args)
    rng = np.random.default_rng(cfg.seed + 99)

    data = simulate(cfg)
    X = build_design(data["q0"], data["q1"], cfg.lags)
    y0 = data["r0"][cfg.lags :]
    y1 = data["r1"][cfg.lags :]

    coef0 = estimate_kernel(X, y0)
    coef1 = estimate_kernel(X, y1)

    boot0 = bootstrap_kernels(X, y0, cfg.lags, cfg.bootstrap, rng) if cfg.bootstrap else np.zeros((1, 2 * cfg.lags))
    boot1 = bootstrap_kernels(X, y1, cfg.lags, cfg.bootstrap, rng) if cfg.bootstrap else np.zeros((1, 2 * cfg.lags))

    summary0 = summarize(coef0, boot0, cfg.lags)
    summary1 = summarize(coef1, boot1, cfg.lags)

    print("Impact kernel summary (asset 0)")
    print("self_lag1:", round(float(summary0["self"][0]), 6), "cross_lag1:", round(float(summary0["cross"][0]), 6))
    print("Impact kernel summary (asset 1)")
    print("self_lag1:", round(float(summary1["self"][0]), 6), "cross_lag1:", round(float(summary1["cross"][0]), 6))

    if args.out:
        np.savez(
            args.out,
            q0=data["q0"],
            q1=data["q1"],
            r0=data["r0"],
            r1=data["r1"],
            coef0=coef0,
            coef1=coef1,
            self0=summary0["self"],
            cross0=summary0["cross"],
            self1=summary1["self"],
            cross1=summary1["cross"],
            self0_lo=summary0["self_lo"],
            self0_hi=summary0["self_hi"],
            cross0_lo=summary0["cross_lo"],
            cross0_hi=summary0["cross_hi"],
            self1_lo=summary1["self_lo"],
            self1_hi=summary1["self_hi"],
            cross1_lo=summary1["cross_lo"],
            cross1_hi=summary1["cross_hi"],
        )
        print(f"saved npz: {args.out}")


if __name__ == "__main__":
    main()
