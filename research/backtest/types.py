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
    sell_tax_bps: float = 0.0  # Sell-side tax (bps). 0 for futures; set >0 for TWSE equities
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
    mid_prices: np.ndarray | None = None
    # --- Provenance metadata (added 2026-04-15) ---
    engine_type: str = "taker"
    fill_model: str = ""
    cost_model: str = ""
    instrument: str = ""
    data_period: str = ""
    data_source: str = ""
    pipeline_mode: str = ""
    created_at: str = ""
    # --- Maker-specific (None for taker) ---
    maker_scorecard: dict | None = None
    per_spread_breakdown: dict | None = None
    queue_fraction: float | None = None
    # --- Slice B: Maker Realism (added 2026-05-05) ---
    # Aggregation policy (set inside ``MakerEngine.run``):
    #   * ``residual_mtm_pts`` -> SUM across traded days of each day's
    #     ``residual_mtm_pts`` (mirrors ``total_gross`` accumulation in the
    #     day loop; the equity curve already reflects this sum).
    #   * ``residual_qty``     -> final-day snapshot, SIGNED (positive=long,
    #     negative=short).  Per-day FIFO is independent so day-to-day residual
    #     qty is not additive.  Sign preserved for accounting.
    #   * ``abs_residual_qty`` -> ``abs(residual_qty)`` derived field for
    #     display / aggregation contexts where sign would mask scale.
    #   * ``mark_method``      -> single-policy string (identical every day
    #     under current single-policy design).
    # All four default to safe zero-values so taker engines and synthetic
    # fixtures that construct ``BacktestResult`` without these fields stay
    # backward-compatible.
    residual_mtm_pts: float = 0.0
    residual_qty: int = 0
    abs_residual_qty: int = 0
    mark_method: str = ""
    # --- Daily detail ---
    daily_pnl: list[dict] | None = None


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


@dataclass(frozen=True)
class CPCVConfig:
    """Combinatorial Purged Cross-Validation configuration."""

    n_groups: int = 6  # N contiguous groups; C(N, N//2) test-set combos
    embargo_pct: float = 0.01  # 1% of data as gap between train/test boundaries
    purge_pct: float = 0.005  # 0.5% purge around test boundaries
    min_group_samples: int = 50  # minimum ticks per group


@dataclass(frozen=True)
class CPCVFoldResult:
    """Result for a single CPCV path (combination of test groups)."""

    path_idx: int
    train_indices: tuple[int, ...]  # which groups used for train
    test_indices: tuple[int, ...]  # which groups used for test
    train_size: int
    test_size: int
    sharpe: float
    ic_mean: float
    max_drawdown: float
    turnover: float


@dataclass(frozen=True)
class CPCVResult:
    """Aggregated result from Combinatorial Purged Cross-Validation."""

    config: CPCVConfig
    n_paths: int  # C(n_groups, n_groups//2)
    folds: list[CPCVFoldResult]
    pbo: float  # Probability of Backtest Overfitting
    path_sharpes: list[float]
    path_consistency_pct: float  # fraction of paths with positive Sharpe
    sharpe_mean: float
    sharpe_std: float
    sharpe_min: float


def _hash_config(config: BacktestConfig) -> str:
    payload = json.dumps(asdict(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
