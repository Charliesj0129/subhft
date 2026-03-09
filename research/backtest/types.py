"""research/backtest/types.py — Shared backtest type definitions.

Extracted from hbt_runner.py in v1.1 so that HftNativeRunner and Gate C
can import types without pulling in the retired ResearchBacktestRunner.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np


@dataclass(frozen=True)
class BacktestConfig:
    data_paths: list[str]
    is_oos_split: float = 0.7
    maker_fee_bps: float = -0.2
    taker_fee_bps: float = 0.2
    signal_threshold: float = 0.3
    max_position: int = 5
    initial_equity: float = 1_000_000.0
    latency_profile_id: str = "sim_p95_v2026-02-26"
    local_decision_pipeline_latency_us: int = 250
    submit_ack_latency_ms: float = 36.0
    modify_ack_latency_ms: float = 43.0
    cancel_ack_latency_ms: float = 47.0
    live_uplift_factor: float = 1.5
    # Stage 4 governance: auto-compute per-regime (high_vol/low_vol) Sharpe
    # breakdown on every backtest run. Disable only for ultra-short datasets
    # (<8 bars) or when calling from walk-forward inner loops.
    auto_regime_split: bool = True
    backtest_engine: str = "hftbacktest_v2"
    queue_model: str = "PowerProbQueueModel(3.0)"
    latency_model: str = "IntpOrderLatency"
    exchange_model: str = "NoPartialFillExchange"
    min_queue_survival_rate: float = 0.3


@dataclass(frozen=True)
class BacktestResult:
    signals: np.ndarray
    equity_curve: np.ndarray
    positions: np.ndarray
    sharpe_is: float
    sharpe_oos: float
    ic_series: np.ndarray
    ic_mean: float
    ic_std: float
    ic_tstat: float
    ic_pvalue: float
    ic_halflife: int
    sortino: float
    cvar_5pct: float
    turnover: float
    max_drawdown: float
    regime_metrics: dict[str, float]
    capacity_estimate: float
    run_id: str
    config_hash: str
    latency_profile: dict[str, Any]


@dataclass(frozen=True)
class WalkForwardConfig:
    n_splits: int = 5
    window_type: str = "expanding"
    min_train_samples: int = 30


@dataclass(frozen=True)
class WalkForwardFoldResult:
    fold_idx: int
    train_size: int
    test_size: int
    sharpe: float
    ic_mean: float
    max_drawdown: float
    turnover: float


@dataclass(frozen=True)
class WalkForwardResult:
    config: WalkForwardConfig
    folds: list[WalkForwardFoldResult]
    fold_sharpe_mean: float
    fold_sharpe_std: float
    fold_sharpe_min: float
    fold_sharpe_max: float
    fold_consistency_pct: float
    fold_ic_mean: float


def _hash_config(config: BacktestConfig) -> str:
    payload = json.dumps(asdict(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

