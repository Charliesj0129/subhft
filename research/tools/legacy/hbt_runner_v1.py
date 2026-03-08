# RETIRED in v1.1 — 僅供歷史參考
# Types: research/backtest/types.py
# Runner: research/backtest/hft_native_runner.py
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from structlog import get_logger

logger = get_logger("hbt_runner")


def _ensure_project_root_on_path() -> None:
    root = Path(__file__).resolve().parents[2]
    if (root / "research").exists():
        root_str = str(root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)


_ensure_project_root_on_path()

from research.backtest.metrics import (  # noqa: E402
    compute_capacity,
    compute_cvar,
    compute_ic,
    compute_ic_halflife,
    compute_ic_ttest,
    compute_max_drawdown,
    compute_sharpe,
    compute_sortino,
    compute_turnover,
)
from research.registry.alpha_registry import AlphaRegistry  # noqa: E402
from research.registry.schemas import AlphaProtocol  # noqa: E402


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


class ResearchBacktestRunner:
    def __init__(self, alpha: AlphaProtocol, config: BacktestConfig):
        import warnings
        warnings.warn(
            "ResearchBacktestRunner is deprecated. "
            "Use HftNativeRunner (research.backtest.hft_native_runner). "
            "Custom numpy simulation will be removed in v1.2.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.alpha = _maybe_wrap_batch_alpha(alpha)
        self.config = config
        self._last_run_returns: np.ndarray | None = None
        self._last_run_path: str | None = None

    def run(self) -> BacktestResult:
        if not self.config.data_paths:
            raise ValueError("BacktestConfig.data_paths is empty")

        signals_chunks: list[np.ndarray] = []
        positions_chunks: list[np.ndarray] = []
        equity_chunks: list[np.ndarray] = []
        returns_chunks: list[np.ndarray] = []
        volume_chunks: list[np.ndarray] = []

        latency_profile = {
            "latency_profile_id": self.config.latency_profile_id,
            "local_decision_pipeline_latency_us": int(self.config.local_decision_pipeline_latency_us),
            "submit_ack_latency_ms": float(self.config.submit_ack_latency_ms),
            "modify_ack_latency_ms": float(self.config.modify_ack_latency_ms),
            "cancel_ack_latency_ms": float(self.config.cancel_ack_latency_ms),
            "live_uplift_factor": float(self.config.live_uplift_factor),
            "model_applied": True,
        }

        current_equity_base = float(self.config.initial_equity)
        for data_path in self.config.data_paths:
            data = self._load_data(data_path)
            price = self._extract_price(data)
            if price.size == 0:
                continue
            volume = self._extract_volume(data, len(price))
            signals = self._generate_signals(data, len(price))
            desired_positions = self._signals_to_positions(signals)
            positions = self._apply_latency_to_positions(data, desired_positions)
            equity = self._compute_equity_curve(price, positions, initial_equity=current_equity_base)
            fwd_returns = self._forward_returns(price)

            current_equity_base = float(equity[-1]) if equity.size else current_equity_base
            signals_chunks.append(signals)
            positions_chunks.append(positions)
            equity_chunks.append(equity)
            returns_chunks.append(fwd_returns)
            volume_chunks.append(volume)

        if not equity_chunks:
            empty = np.zeros(0, dtype=np.float64)
            return BacktestResult(
                signals=empty,
                equity_curve=np.asarray([self.config.initial_equity], dtype=np.float64),
                positions=empty,
                sharpe_is=0.0,
                sharpe_oos=0.0,
                ic_series=empty,
                ic_mean=0.0,
                ic_std=0.0,
                ic_tstat=0.0,
                ic_pvalue=1.0,
                ic_halflife=0,
                sortino=0.0,
                cvar_5pct=0.0,
                turnover=0.0,
                max_drawdown=0.0,
                regime_metrics={},
                capacity_estimate=0.0,
                run_id=str(uuid.uuid4()),
                config_hash=_hash_config(self.config),
                latency_profile=latency_profile,
            )

        signals = np.concatenate(signals_chunks) if len(signals_chunks) > 1 else signals_chunks[0]
        positions = np.concatenate(positions_chunks) if len(positions_chunks) > 1 else positions_chunks[0]
        equity = np.concatenate(equity_chunks) if len(equity_chunks) > 1 else equity_chunks[0]
        fwd_returns = np.concatenate(returns_chunks) if len(returns_chunks) > 1 else returns_chunks[0]
        volume = np.concatenate(volume_chunks) if len(volume_chunks) > 1 else volume_chunks[0]

        self._last_run_returns = fwd_returns
        self._last_run_path = "|".join(self.config.data_paths)

        split = max(2, int(len(equity) * self.config.is_oos_split))
        split = min(split, len(equity) - 1) if len(equity) > 2 else len(equity)

        sharpe_is = compute_sharpe(equity[:split]) if split >= 2 else 0.0
        sharpe_oos = compute_sharpe(equity[split:]) if split >= 2 else 0.0
        # IC on OOS slice only — avoids in-sample look-ahead contamination
        ic_mean, ic_std, ic_series = compute_ic(signals[split:], fwd_returns[split:])
        ic_tstat, ic_pvalue = compute_ic_ttest(ic_series)
        ic_halflife = compute_ic_halflife(signals)
        turnover = compute_turnover(positions)
        max_dd = compute_max_drawdown(equity)
        sortino = compute_sortino(equity[split:]) if split >= 2 else 0.0
        cvar_5 = compute_cvar(equity[split:], alpha=0.05) if split >= 2 else 0.0
        capacity = compute_capacity(positions, volume)
        regime = self._regime_metrics(fwd_returns, positions) if self.config.auto_regime_split else {}

        return BacktestResult(
            signals=signals,
            equity_curve=equity,
            positions=positions,
            sharpe_is=sharpe_is,
            sharpe_oos=sharpe_oos,
            ic_series=ic_series,
            ic_mean=ic_mean,
            ic_std=ic_std,
            ic_tstat=ic_tstat,
            ic_pvalue=ic_pvalue,
            ic_halflife=ic_halflife,
            sortino=sortino,
            cvar_5pct=cvar_5,
            turnover=turnover,
            max_drawdown=max_dd,
            regime_metrics=regime,
            capacity_estimate=capacity,
            run_id=str(uuid.uuid4()),
            config_hash=_hash_config(self.config),
            latency_profile=latency_profile,
        )

    def run_walk_forward(
        self,
        alpha: AlphaProtocol,
        config: WalkForwardConfig | None = None,
    ) -> WalkForwardResult:
        wf = config or WalkForwardConfig()
        if wf.window_type != "expanding":
            raise ValueError(f"Unsupported walk-forward window_type: {wf.window_type!r}")
        if wf.n_splits <= 0:
            raise ValueError("WalkForwardConfig.n_splits must be > 0")

        full = self._concatenate_data_paths()
        total_rows = int(full.shape[0]) if full.ndim > 0 else int(full.size)
        if total_rows <= 1:
            return WalkForwardResult(
                config=wf,
                folds=[],
                fold_sharpe_mean=float("nan"),
                fold_sharpe_std=float("nan"),
                fold_sharpe_min=float("nan"),
                fold_sharpe_max=float("nan"),
                fold_consistency_pct=float("nan"),
                fold_ic_mean=float("nan"),
            )

        fold_size = total_rows // (int(wf.n_splits) + 1)
        if fold_size <= 0:
            return WalkForwardResult(
                config=wf,
                folds=[],
                fold_sharpe_mean=float("nan"),
                fold_sharpe_std=float("nan"),
                fold_sharpe_min=float("nan"),
                fold_sharpe_max=float("nan"),
                fold_consistency_pct=float("nan"),
                fold_ic_mean=float("nan"),
            )

        eval_alpha = _maybe_wrap_batch_alpha(alpha)
        orig_alpha = self.alpha
        self.alpha = eval_alpha
        folds: list[WalkForwardFoldResult] = []
        try:
            for fold_idx in range(int(wf.n_splits)):
                train_end = fold_size * (fold_idx + 1)
                test_end = fold_size * (fold_idx + 2)
                train = full[:train_end]
                test = full[train_end:test_end]
                train_size = int(train.shape[0]) if train.ndim > 0 else int(train.size)
                test_size = int(test.shape[0]) if test.ndim > 0 else int(test.size)
                if train_size < int(wf.min_train_samples) or test_size < 2:
                    continue

                eval_alpha.reset()
                _ = self._generate_signals(train, train_size, reset_alpha=False)
                test_signals = self._generate_signals(test, test_size, reset_alpha=False)
                desired_positions = self._signals_to_positions(test_signals)
                positions = self._apply_latency_to_positions(test, desired_positions)
                price = self._extract_price(test)
                equity = self._equity_from_positions(price, positions, initial_equity=self.config.initial_equity)
                fold_metrics = self._metrics_from_equity(
                    equity=equity,
                    signals=test_signals,
                    price=price,
                    positions=positions,
                )
                folds.append(
                    WalkForwardFoldResult(
                        fold_idx=fold_idx,
                        train_size=train_size,
                        test_size=test_size,
                        sharpe=float(fold_metrics["sharpe"]),
                        ic_mean=float(fold_metrics["ic_mean"]),
                        max_drawdown=float(fold_metrics["max_drawdown"]),
                        turnover=float(fold_metrics["turnover"]),
                    )
                )
        finally:
            self.alpha = orig_alpha

        if not folds:
            return WalkForwardResult(
                config=wf,
                folds=[],
                fold_sharpe_mean=float("nan"),
                fold_sharpe_std=float("nan"),
                fold_sharpe_min=float("nan"),
                fold_sharpe_max=float("nan"),
                fold_consistency_pct=float("nan"),
                fold_ic_mean=float("nan"),
            )

        fold_sharpes = np.asarray([row.sharpe for row in folds], dtype=np.float64)
        fold_ics = np.asarray([row.ic_mean for row in folds], dtype=np.float64)
        consistency = float(np.mean(fold_sharpes > 0.0)) if fold_sharpes.size else float("nan")
        return WalkForwardResult(
            config=wf,
            folds=folds,
            fold_sharpe_mean=float(np.mean(fold_sharpes)),
            fold_sharpe_std=float(np.std(fold_sharpes)),
            fold_sharpe_min=float(np.min(fold_sharpes)),
            fold_sharpe_max=float(np.max(fold_sharpes)),
            fold_consistency_pct=consistency,
            fold_ic_mean=float(np.mean(fold_ics)) if fold_ics.size else float("nan"),
        )

    def run_latency_sweep(
        self,
        percentiles: tuple[str, ...] = ("P50", "P75", "P95", "P99"),
    ) -> dict[str, BacktestResult]:
        """Run the same backtest under multiple latency assumptions.

        Uses Shioaji-derived latency baseline multipliers:
          P50: 0.7x,  P75: 0.85x,  P95: 1.0x (default),  P99: 1.2x
        relative to the config's base latencies.
        """
        multipliers = {"P50": 0.7, "P75": 0.85, "P95": 1.0, "P99": 1.2}
        results: dict[str, BacktestResult] = {}
        base_cfg = self.config
        for pct in percentiles:
            mult = multipliers.get(pct, 1.0)
            swept = BacktestConfig(
                data_paths=base_cfg.data_paths,
                is_oos_split=base_cfg.is_oos_split,
                maker_fee_bps=base_cfg.maker_fee_bps,
                taker_fee_bps=base_cfg.taker_fee_bps,
                signal_threshold=base_cfg.signal_threshold,
                max_position=base_cfg.max_position,
                initial_equity=base_cfg.initial_equity,
                latency_profile_id=f"{base_cfg.latency_profile_id}_{pct}",
                local_decision_pipeline_latency_us=base_cfg.local_decision_pipeline_latency_us,
                submit_ack_latency_ms=base_cfg.submit_ack_latency_ms * mult,
                modify_ack_latency_ms=base_cfg.modify_ack_latency_ms * mult,
                cancel_ack_latency_ms=base_cfg.cancel_ack_latency_ms * mult,
                live_uplift_factor=base_cfg.live_uplift_factor,
            )
            runner = ResearchBacktestRunner(self.alpha, swept)
            results[pct] = runner.run()
        return results

    def run_regime_split(self) -> dict[str, BacktestResult]:
        base = self.run()
        if base.signals.size < 16:
            return {"all": base}

        if self._last_run_returns is not None and self._last_run_returns.size >= base.signals.size:
            returns = self._last_run_returns
        else:
            returns_rows: list[np.ndarray] = []
            for path in self.config.data_paths:
                data = self._load_data(path)
                returns_rows.append(self._forward_returns(self._extract_price(data)))
            returns = np.concatenate(returns_rows) if returns_rows else np.zeros(0, dtype=np.float64)
        vol = np.abs(returns)
        q = float(np.quantile(vol, 0.7))
        high_mask = vol >= q
        low_mask = ~high_mask

        out: dict[str, BacktestResult] = {"all": base}
        for regime, mask in (("high_vol", high_mask), ("low_vol", low_mask)):
            sliced = self._slice_result(base, mask)
            if sliced is not None:
                out[regime] = sliced
        return out

    def _load_data(self, path: str) -> np.ndarray:
        data = np.load(path, allow_pickle=False)
        if isinstance(data, np.lib.npyio.NpzFile):
            if "data" not in data:
                raise ValueError(f"NPZ file missing 'data' key: {path}")
            return np.asarray(data["data"])
        return np.asarray(data)

    def _extract_price(self, data: np.ndarray) -> np.ndarray:
        if data.dtype.names:
            for field in ("current_mid", "mid", "mid_price", "px", "price", "close"):
                if field in data.dtype.names:
                    return np.asarray(data[field], dtype=np.float64)
            for bid_key, ask_key in (
                ("best_bid", "best_ask"),
                ("bid_px", "ask_px"),
                ("bid_price", "ask_price"),
            ):
                if bid_key in data.dtype.names and ask_key in data.dtype.names:
                    bid = np.asarray(data[bid_key], dtype=np.float64)
                    ask = np.asarray(data[ask_key], dtype=np.float64)
                    return (bid + ask) / 2.0
            raise ValueError(
                "Unable to extract price from structured data. "
                f"Available fields: {tuple(data.dtype.names)}"
            )
        return np.asarray(data, dtype=np.float64).reshape(-1)

    def _extract_volume(self, data: np.ndarray, n: int) -> np.ndarray:
        if data.dtype.names:
            for field in ("trade_vol", "qty", "volume", "trade_qty"):
                if field in data.dtype.names:
                    return np.asarray(data[field], dtype=np.float64)[:n]
        return np.ones(n, dtype=np.float64)

    def _generate_signals(self, data: np.ndarray, n: int, *, reset_alpha: bool = True) -> np.ndarray:
        if reset_alpha:
            self.alpha.reset()
        signals = np.zeros(n, dtype=np.float64)

        update_batch = getattr(self.alpha, "update_batch", None)
        if callable(update_batch):
            try:
                batch_out = np.asarray(update_batch(data), dtype=np.float64).reshape(-1)
                if batch_out.size >= n:
                    signals[:] = batch_out[:n]
                    return signals
            except Exception as exc:
                logger.warning("batch_alpha_failed_falling_back_to_row_wise", reason=str(exc), exc_info=True)

        if data.dtype.names:
            field_names = tuple(data.dtype.names)
            base_keys = set(field_names)
            payload: dict[str, Any] = {name: 0.0 for name in field_names}
            for i in range(n):
                row = data[i]
                for name in field_names:
                    payload[name] = _to_python_scalar(row[name])
                _with_standard_aliases_inplace(payload, base_keys)
                signals[i] = float(self.alpha.update(**payload))
            return signals

        flat = np.asarray(data, dtype=np.float64).reshape(-1)
        for i, value in enumerate(flat):
            signals[i] = float(self.alpha.update(value=value))
        return signals

    def _concatenate_data_paths(self) -> np.ndarray:
        chunks: list[np.ndarray] = []
        for data_path in self.config.data_paths:
            chunk = self._load_data(data_path)
            if chunk.size <= 0:
                continue
            chunks.append(chunk)
        if not chunks:
            return np.asarray([], dtype=np.float64)
        if len(chunks) == 1:
            return chunks[0]
        return np.concatenate(chunks)

    def _equity_from_positions(
        self,
        price: np.ndarray,
        positions: np.ndarray,
        *,
        initial_equity: float | None = None,
    ) -> np.ndarray:
        return self._compute_equity_curve(price, positions, initial_equity=initial_equity)

    def _metrics_from_equity(
        self,
        *,
        equity: np.ndarray,
        signals: np.ndarray,
        price: np.ndarray,
        positions: np.ndarray,
    ) -> dict[str, float]:
        sharpe = compute_sharpe(equity) if equity.size >= 2 else 0.0
        fwd_returns = self._forward_returns(price)
        ic_mean, _, _ = compute_ic(signals, fwd_returns)
        max_dd = compute_max_drawdown(equity)
        turnover = compute_turnover(positions)
        return {
            "sharpe": float(sharpe),
            "ic_mean": float(ic_mean),
            "max_drawdown": float(max_dd),
            "turnover": float(turnover),
        }

    def _signals_to_positions(self, signals: np.ndarray) -> np.ndarray:
        threshold = float(self.config.signal_threshold)
        max_pos = int(self.config.max_position)
        positions = np.zeros_like(signals)
        for i in range(1, len(signals)):
            prev = positions[i - 1]
            sig = signals[i]
            if sig > threshold:
                positions[i] = min(prev + 1, max_pos)
            elif sig < -threshold:
                positions[i] = max(prev - 1, -max_pos)
            else:
                positions[i] = prev
        return positions

    def _compute_equity_curve(
        self,
        price: np.ndarray,
        positions: np.ndarray,
        *,
        initial_equity: float | None = None,
    ) -> np.ndarray:
        n = min(price.size, positions.size)
        if n < 2:
            base = self.config.initial_equity if initial_equity is None else float(initial_equity)
            return np.asarray([base], dtype=np.float64)
        px = price[:n]
        pos = positions[:n]
        base = self.config.initial_equity if initial_equity is None else float(initial_equity)

        pnl_step = pos[:-1] * np.diff(px)
        turnover = np.abs(np.diff(pos, prepend=0))
        fee_rate = max(self.config.taker_fee_bps, 0.0) / 10_000.0
        fee_step = turnover[1:] * np.abs(px[1:]) * fee_rate
        pnl_after_fee = pnl_step - fee_step
        pnl_cum = np.cumsum(pnl_after_fee, dtype=np.float64)

        equity = np.empty(n, dtype=np.float64)
        equity[0] = base
        equity[1:] = base + pnl_cum
        return equity

    def _apply_latency_to_positions(self, data: np.ndarray, desired_positions: np.ndarray) -> np.ndarray:
        n = int(desired_positions.size)
        if n <= 1:
            return np.asarray(desired_positions, dtype=np.float64)

        step_ns = self._estimate_step_ns(data)
        upl = max(1.0, float(self.config.live_uplift_factor))
        local_ns = max(0, int(self.config.local_decision_pipeline_latency_us)) * 1_000
        submit_ns = int(float(self.config.submit_ack_latency_ms) * 1_000_000 * upl)
        modify_ns = int(float(self.config.modify_ack_latency_ms) * 1_000_000 * upl)
        cancel_ns = int(float(self.config.cancel_ack_latency_ms) * 1_000_000 * upl)

        submit_steps = max(1, int(np.ceil((local_ns + submit_ns) / max(1, step_ns))))
        modify_steps = max(1, int(np.ceil((local_ns + modify_ns) / max(1, step_ns))))
        cancel_steps = max(1, int(np.ceil((local_ns + cancel_ns) / max(1, step_ns))))

        executed = np.zeros(n, dtype=np.float64)
        pending_due = -1
        pending_target = 0.0

        for i in range(1, n):
            executed[i] = executed[i - 1]
            if pending_due >= 0 and i >= pending_due:
                executed[i] = pending_target
                pending_due = -1

            target = float(desired_positions[i])
            # Only submit a new order when the desired position actually changes.
            # Checking prev_exec != target would re-submit every tick while awaiting
            # fill, causing infinite deferral (order always overwritten before arriving).
            if target == float(desired_positions[i - 1]):
                continue

            prev_exec = float(executed[i])
            if target == prev_exec:
                pending_due = -1  # already at new target; cancel any pending order
                continue

            steps = submit_steps
            if prev_exec != 0.0 and target == 0.0:
                steps = cancel_steps
            elif prev_exec != 0.0 and np.sign(prev_exec) != np.sign(target):
                steps = modify_steps
            elif prev_exec != 0.0 and abs(target) < abs(prev_exec):
                steps = cancel_steps

            pending_due = min(n - 1, i + steps)
            pending_target = target

        return executed

    def _estimate_step_ns(self, data: np.ndarray) -> int:
        if data.dtype.names:
            for field in ("local_ts", "exch_ts"):
                if field in data.dtype.names:
                    ts = np.asarray(data[field], dtype=np.int64).reshape(-1)
                    if ts.size >= 2:
                        diff = np.diff(ts)
                        positive = diff[diff > 0]
                        if positive.size > 0:
                            return int(np.median(positive))
        # Fall back to 1ms bars when timestamp cadence is unavailable.
        return 1_000_000

    def _forward_returns(self, price: np.ndarray) -> np.ndarray:
        if price.size < 2:
            return np.zeros_like(price)
        out = np.zeros(price.size, dtype=np.float64)
        base = price[:-1]
        diff = np.diff(price)
        out[:-1] = np.divide(diff, base, out=np.zeros_like(diff), where=base != 0)
        return out

    def _regime_metrics(self, returns: np.ndarray, positions: np.ndarray) -> dict[str, float]:
        if returns.size < 8:
            return {}

        vol = np.abs(returns)
        median = float(np.median(vol))
        high = vol >= median
        low = ~high

        metrics: dict[str, float] = {}
        for name, mask in (("high_vol", high), ("low_vol", low)):
            if np.count_nonzero(mask) < 4:
                continue
            masked_returns = returns[mask] * positions[: len(returns)][mask]
            metrics[name] = _safe_sharpe_from_returns(masked_returns)
        return metrics

    def _slice_result(self, base: BacktestResult, mask: np.ndarray) -> BacktestResult | None:
        n = min(mask.size, base.signals.size, base.positions.size, base.equity_curve.size)
        if n < 8:
            return None
        local_mask = mask[:n]
        if np.count_nonzero(local_mask) < 8:
            return None

        sig = base.signals[:n][local_mask]
        pos = base.positions[:n][local_mask]
        eq = base.equity_curve[:n][local_mask]
        if eq.size < 2:
            return None
        ic_mean, ic_std, ic_series = compute_ic(sig, np.diff(eq, prepend=eq[0]))
        sharpe = compute_sharpe(eq)
        ic_t, ic_p = compute_ic_ttest(ic_series)
        return BacktestResult(
            signals=sig,
            equity_curve=eq,
            positions=pos,
            sharpe_is=sharpe,
            sharpe_oos=sharpe,
            ic_series=ic_series,
            ic_mean=ic_mean,
            ic_std=ic_std,
            ic_tstat=ic_t,
            ic_pvalue=ic_p,
            ic_halflife=compute_ic_halflife(sig),
            sortino=compute_sortino(eq),
            cvar_5pct=compute_cvar(eq, alpha=0.05),
            turnover=compute_turnover(pos),
            max_drawdown=compute_max_drawdown(eq),
            regime_metrics={},
            capacity_estimate=base.capacity_estimate,
            run_id=base.run_id,
            config_hash=base.config_hash,
            latency_profile=base.latency_profile,
        )


class _BatchAlphaAdapter:
    """Compatibility adapter that formalizes a batch API for row-wise alphas.

    The adapter preserves the original alpha semantics (same `update(**row)` order)
    while exposing `update_batch(data)` so `ResearchBacktestRunner` can treat batch
    alphas as the primary contract.
    """

    def __init__(self, alpha: AlphaProtocol):
        self._alpha = alpha
        self.manifest = alpha.manifest
        manifest_fields = tuple(getattr(self.manifest, "data_fields", ()) or ())
        self._required_fields = tuple(str(f) for f in manifest_fields if f)

    def reset(self) -> None:
        self._alpha.reset()

    def update(self, *args: Any, **kwargs: Any) -> float:
        return float(self._alpha.update(*args, **kwargs))

    def get_signal(self) -> float:
        return float(self._alpha.get_signal())

    def update_batch(self, data: np.ndarray) -> np.ndarray:
        arr = np.asarray(data)
        n = int(arr.shape[0]) if arr.ndim > 0 else int(arr.size)
        out = np.zeros(n, dtype=np.float64)
        if n <= 0:
            return out

        if arr.dtype.names:
            field_names = tuple(arr.dtype.names)
            selected = tuple(name for name in field_names if not self._required_fields or name in self._required_fields)
            if not selected:
                selected = field_names
            base_keys = set(selected)
            payload: dict[str, Any] = {name: 0.0 for name in selected}
            for i in range(n):
                row = arr[i]
                for name in selected:
                    payload[name] = _to_python_scalar(row[name])
                _with_standard_aliases_inplace(payload, base_keys)
                out[i] = float(self._alpha.update(**payload))
            return out

        flat = np.asarray(arr, dtype=np.float64).reshape(-1)
        for i, value in enumerate(flat):
            out[i] = float(self._alpha.update(value=float(value)))
        return out


def _maybe_wrap_batch_alpha(alpha: AlphaProtocol) -> AlphaProtocol:
    """Promote batch API to the default contract using an adapter when needed."""
    force_disable = os.getenv("HFT_RESEARCH_BATCH_ALPHA_ADAPTER", "1").strip().lower() in {"0", "false", "no", "off"}
    if force_disable:
        return alpha
    update_batch = getattr(alpha, "update_batch", None)
    if callable(update_batch):
        return alpha
    try:
        return _BatchAlphaAdapter(alpha)
    except Exception:
        return alpha


def _safe_sharpe_from_returns(returns: np.ndarray) -> float:
    if returns.size < 2:
        return 0.0
    std = float(np.std(returns))
    if std == 0.0:
        return 0.0
    return float(np.mean(returns) / std * np.sqrt(252.0))


def _to_python_scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    return value


def _hash_config(config: BacktestConfig) -> str:
    payload = json.dumps(asdict(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _with_standard_aliases_inplace(payload: dict[str, Any], base_keys: set[str]) -> dict[str, Any]:
    if "bid_px" not in base_keys:
        payload["bid_px"] = payload.get("best_bid", payload.get("bid_price", payload.get("bid")))
    if "ask_px" not in base_keys:
        payload["ask_px"] = payload.get("best_ask", payload.get("ask_price", payload.get("ask")))
    if "bid_qty" not in base_keys:
        payload["bid_qty"] = payload.get("bid_depth", payload.get("bid_size", payload.get("bqty", 0.0)))
    if "ask_qty" not in base_keys:
        payload["ask_qty"] = payload.get("ask_depth", payload.get("ask_size", payload.get("aqty", 0.0)))
    if "trade_vol" not in base_keys:
        payload["trade_vol"] = payload.get("qty", payload.get("volume", payload.get("trade_qty", 0.0)))
    if "current_mid" not in base_keys:
        bid_px = payload.get("bid_px")
        ask_px = payload.get("ask_px")
        if bid_px is not None and ask_px is not None:
            payload["current_mid"] = (float(bid_px) + float(ask_px)) / 2.0
        else:
            payload["current_mid"] = payload.get("mid", payload.get("mid_price", payload.get("price", 0.0)))
    return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run standardized research backtest.")
    parser.add_argument("--alpha", required=True, help="alpha_id registered under research/alphas")
    parser.add_argument("--data", required=True, nargs="+", help="Path(s) to npy/npz data file(s)")
    parser.add_argument("--signal-threshold", type=float, default=0.3)
    parser.add_argument("--max-position", type=int, default=5)
    parser.add_argument("--is-oos-split", type=float, default=0.7)
    parser.add_argument("--out", default="", help="Optional JSON output path for summary metrics")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    registry = AlphaRegistry()
    loaded = registry.discover("research/alphas")
    alpha = loaded.get(args.alpha)
    if alpha is None:
        known = ", ".join(sorted(loaded))
        raise SystemExit(f"Unknown alpha_id '{args.alpha}'. Known: {known}")

    config = BacktestConfig(
        data_paths=[str(Path(p)) for p in args.data],
        signal_threshold=float(args.signal_threshold),
        max_position=int(args.max_position),
        is_oos_split=float(args.is_oos_split),
    )
    from research.backtest.hft_native_runner import HftNativeRunner, ensure_hftbt_npz
    for p in config.data_paths:
        ensure_hftbt_npz(p)
    result = HftNativeRunner(alpha, config).run()
    summary = {
        "alpha_id": alpha.manifest.alpha_id,
        "run_id": result.run_id,
        "config_hash": result.config_hash,
        "sharpe_is": result.sharpe_is,
        "sharpe_oos": result.sharpe_oos,
        "ic_mean": result.ic_mean,
        "ic_std": result.ic_std,
        "ic_tstat": result.ic_tstat,
        "ic_pvalue": result.ic_pvalue,
        "ic_halflife": result.ic_halflife,
        "sortino": result.sortino,
        "cvar_5pct": result.cvar_5pct,
        "turnover": result.turnover,
        "max_drawdown": result.max_drawdown,
        "capacity_estimate": result.capacity_estimate,
        "regime_metrics": result.regime_metrics,
    }
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
