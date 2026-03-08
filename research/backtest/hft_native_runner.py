"""hft_native_runner.py — BacktestResult via hftbacktest native engine.

Part C of the dirty-data-repair + golden-data pipeline plan.

HftNativeRunner uses HftBacktestAdapter (true hftbacktest simulation) and produces
a BacktestResult compatible with Gate C.

Data flow:
    hftbt.npz (event_dtype)
        └─> IS slice (temp npz)
        └─> OOS slice (temp npz)
    Each slice:
        HftBacktestAdapter(AlphaStrategyBridge) → equity_values
        AlphaStrategyBridge.signal_log         → signals + mid_prices
    Metrics computed with research/backtest/metrics.py (same as ResearchBacktestRunner)
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Any

import numpy as np

# Ensure project root is on sys.path (mirrors hbt_runner.py pattern)
def _ensure_project_root_on_path() -> None:
    root = Path(__file__).resolve().parents[2]
    if (root / "research").exists():
        root_str = str(root)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)

_ensure_project_root_on_path()

from research.backtest.alpha_strategy_bridge import AlphaStrategyBridge, signal_log_to_arrays  # noqa: E402
from research.backtest.types import (  # noqa: E402
    BacktestConfig,
    BacktestResult,
    WalkForwardConfig,
    WalkForwardFoldResult,
    WalkForwardResult,
    _hash_config,
)
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
from research.registry.schemas import AlphaProtocol  # noqa: E402

try:
    from hft_platform.backtest.adapter import HftBacktestAdapter
    _ADAPTER_AVAILABLE = True
except ImportError:
    _ADAPTER_AVAILABLE = False

try:
    from hftbacktest.types import event_dtype as _HBT_EVENT_DTYPE
    _HFTBT_AVAILABLE = True
except ImportError:
    _HBT_EVENT_DTYPE = None
    _HFTBT_AVAILABLE = False


# ---------------------------------------------------------------------------
# NPZ splitting helper
# ---------------------------------------------------------------------------
def _split_npz(path: str, split: float = 0.7) -> tuple[str, str]:
    """Split a hftbacktest NPZ file into IS and OOS temp files.

    Args:
        path: Path to hftbt.npz file containing event_dtype array.
        split: Fraction of rows for IS (default 0.7 = 70%).

    Returns:
        (is_path, oos_path) — paths to temporary NPZ files.
        Caller must clean up these files after use.
    """
    data = np.load(path, allow_pickle=False)
    if isinstance(data, np.lib.npyio.NpzFile):
        arr = np.asarray(data["data"])
    else:
        arr = np.asarray(data)

    n = len(arr)
    split_idx = max(1, min(n - 1, int(n * split)))

    is_arr = arr[:split_idx]
    oos_arr = arr[split_idx:]

    tmp_dir = tempfile.mkdtemp(prefix="hftnative_")
    is_path = os.path.join(tmp_dir, "is.npz")
    oos_path = os.path.join(tmp_dir, "oos.npz")
    np.savez_compressed(is_path, data=is_arr)
    np.savez_compressed(oos_path, data=oos_arr)
    return is_path, oos_path


def _resolve_hftbt_path(data_path: str) -> str | None:
    """Given a data path (possibly research.npy), find the sibling hftbt.npz.

    Checks:
      1. data_path itself if it ends with hftbt.npz
      2. parent_dir/hftbt.npz
    Returns None if not found.
    """
    p = Path(data_path)
    if p.name == "hftbt.npz" and p.exists():
        return str(p)
    sibling = p.parent / "hftbt.npz"
    if sibling.exists():
        return str(sibling)
    return None


def ensure_hftbt_npz(data_path: str) -> str:
    """Auto-convert research.npy → hftbt.npz if not already present.

    Idempotent: if a sibling hftbt.npz already exists, returns its path immediately
    without requiring hftbacktest to be installed.

    Raises ImportError if hftbacktest is not installed and conversion is needed.
    Raises ValueError if data has no recognisable price fields or all rows are zero.
    Raises FileNotFoundError / OSError if data_path does not exist.

    Returns: absolute path to the hftbt.npz file.
    """
    # Fast path: sibling hftbt.npz already exists — no import needed
    hbt_path = _resolve_hftbt_path(data_path)
    if hbt_path is not None:
        return hbt_path

    # Conversion requires hftbacktest
    if not _HFTBT_AVAILABLE:
        raise ImportError(
            "ensure_hftbt_npz requires hftbacktest. Install with: pip install hftbacktest"
        )

    from hftbacktest.types import (  # type: ignore[import]
        BUY_EVENT,
        DEPTH_EVENT,
        EXCH_EVENT,
        LOCAL_EVENT,
        SELL_EVENT,
        TRADE_EVENT,
        event_dtype as _evt_dtype,
    )
    from hft_platform.backtest.convert import _build_event  # reuse tuple format

    # Load research.npy / npz
    raw = np.load(data_path, allow_pickle=False)
    if isinstance(raw, np.lib.npyio.NpzFile):
        arr = np.asarray(raw["data"])
    else:
        arr = np.asarray(raw)

    names: tuple[str, ...] = arr.dtype.names or ()
    has_bid = "bid_px" in names
    has_ask = "ask_px" in names
    if not (has_bid or has_ask):
        raise ValueError(
            f"Data at '{data_path}' has no recognisable price fields (bid_px, ask_px)."
        )

    n = len(arr)
    zeros_f = np.zeros(n, dtype=np.float64)
    bid_px = arr["bid_px"].astype(np.float64) if has_bid else zeros_f.copy()
    ask_px = arr["ask_px"].astype(np.float64) if has_ask else zeros_f.copy()
    bid_qty = arr["bid_qty"].astype(np.float64) if "bid_qty" in names else np.ones(n, dtype=np.float64)
    ask_qty = arr["ask_qty"].astype(np.float64) if "ask_qty" in names else np.ones(n, dtype=np.float64)
    volume = arr["volume"].astype(np.float64) if "volume" in names else zeros_f.copy()

    if "local_ts" in names:
        local_ts = arr["local_ts"].astype(np.int64)
    else:
        local_ts = np.arange(n, dtype=np.int64) * 1_000_000  # 1ms fallback

    bid_ev_code = int(DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | BUY_EVENT)
    ask_ev_code = int(DEPTH_EVENT | EXCH_EVENT | LOCAL_EVENT | SELL_EVENT)
    trade_ev_code = int(TRADE_EVENT | EXCH_EVENT | LOCAL_EVENT)

    events: list[tuple] = []
    for i in range(n):
        bp = float(bid_px[i])
        ap = float(ask_px[i])
        bq = float(bid_qty[i])
        aq = float(ask_qty[i])
        vol = float(volume[i])
        ts = int(local_ts[i])

        # Skip rows where both prices are zero
        if bp == 0.0 and ap == 0.0:
            continue

        events.append(_build_event(bid_ev_code, ts, ts, bp, bq))
        events.append(_build_event(ask_ev_code, ts, ts, ap, aq))
        if vol > 0.0:
            mid = (bp + ap) / 2.0
            events.append(_build_event(trade_ev_code, ts, ts, mid, vol))

    if not events:
        raise ValueError(
            f"No valid events generated from '{data_path}' — all rows had zero prices."
        )

    event_arr = np.array(events, dtype=_evt_dtype)
    out_path = Path(data_path).parent / "hftbt.npz"
    np.savez_compressed(str(out_path), data=event_arr)
    return str(out_path)


# ---------------------------------------------------------------------------
# Single-run helper
# ---------------------------------------------------------------------------
def _run_adapter_slice(
    alpha: AlphaProtocol,
    npz_path: str,
    config: BacktestConfig,
    symbol: str = "ASSET",
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Run HftBacktestAdapter on one NPZ slice.

    Returns:
        equity: float64 array from adapter.equity_values (sampled)
        signals: float64 array from bridge.signal_log
        mid_prices: float64 array from bridge.signal_log
        positions: float64 array (signals→positions via config threshold)
    """
    if not _ADAPTER_AVAILABLE:
        raise ImportError(
            "hft_platform.backtest.adapter.HftBacktestAdapter not available. "
            "Ensure hftbacktest is installed."
        )

    latency_us = int(
        (config.local_decision_pipeline_latency_us
         + config.submit_ack_latency_ms * 1000)
    )
    bridge = AlphaStrategyBridge(
        alpha=alpha,
        max_position=config.max_position,
        signal_threshold=config.signal_threshold,
        symbol=symbol,
    )
    bridge.reset()

    adapter = HftBacktestAdapter(
        strategy=bridge,
        asset_symbol=symbol,
        data_path=npz_path,
        latency_us=latency_us,
        maker_fee=float(config.maker_fee_bps) / 10_000.0,
        taker_fee=float(config.taker_fee_bps) / 10_000.0,
        equity_sample_ns=1_000_000,   # 1ms equity samples
        feature_mode="stats_only",
        queue_model=getattr(config, "queue_model", "PowerProbQueueModel(3.0)"),
        latency_model=getattr(config, "latency_model", "ConstantLatency"),
        exchange_model=getattr(config, "exchange_model", "NoPartialFillExchange"),
    )
    adapter.run()

    equity = adapter.equity_values  # float64 array (sampled)

    _, signals, mid_prices = signal_log_to_arrays(bridge.signal_log)
    positions = _signals_to_positions(signals, config.signal_threshold, config.max_position)

    return equity, signals, mid_prices, positions


def _signals_to_positions(
    signals: np.ndarray,
    threshold: float,
    max_position: int,
) -> np.ndarray:
    """Convert signals to integer position ladder."""
    positions = np.zeros_like(signals)
    for i in range(1, len(signals)):
        prev = positions[i - 1]
        sig = signals[i]
        if sig > threshold:
            positions[i] = min(prev + 1, max_position)
        elif sig < -threshold:
            positions[i] = max(prev - 1, -max_position)
        else:
            positions[i] = prev
    return positions


def _forward_returns(mid_prices: np.ndarray) -> np.ndarray:
    if mid_prices.size < 2:
        return np.zeros_like(mid_prices)
    out = np.zeros(mid_prices.size, dtype=np.float64)
    base = mid_prices[:-1]
    diff = np.diff(mid_prices)
    out[:-1] = np.divide(diff, base, out=np.zeros_like(diff), where=base != 0)
    return out


def _regime_metrics(signals: np.ndarray, fwd_returns: np.ndarray) -> dict[str, float]:
    if signals.size < 8:
        return {}
    vol = np.abs(fwd_returns[:signals.size])
    median = float(np.median(vol))
    out: dict[str, float] = {}
    for name, mask in (("high_vol", vol >= median), ("low_vol", vol < median)):
        count = int(np.count_nonzero(mask))
        if count < 4:
            continue
        masked = fwd_returns[:signals.size][mask] * signals[mask]
        std = float(np.std(masked))
        if std > 0:
            out[name] = float(np.mean(masked) / std * np.sqrt(252.0))
    return out


# ---------------------------------------------------------------------------
# HftNativeRunner
# ---------------------------------------------------------------------------
class HftNativeRunner:
    """Gate C backtest runner using hftbacktest native engine.

    Accepts the same BacktestConfig as ResearchBacktestRunner and returns
    the same BacktestResult interface. Falls back gracefully if hftbacktest
    is not installed.

    Data paths in config.data_paths may point to either:
      - research.npy (the runner resolves the sibling hftbt.npz)
      - hftbt.npz directly
    """

    def __init__(self, alpha: AlphaProtocol, config: BacktestConfig, symbol: str = "ASSET"):
        self.alpha = alpha
        self.config = config
        self.symbol = symbol

    def run(self) -> BacktestResult:
        if not _ADAPTER_AVAILABLE or not _HFTBT_AVAILABLE:
            raise ImportError(
                "HftNativeRunner requires hftbacktest. "
                "Install with: pip install hftbacktest"
            )

        latency_profile = {
            "latency_profile_id": self.config.latency_profile_id,
            "local_decision_pipeline_latency_us": int(self.config.local_decision_pipeline_latency_us),
            "submit_ack_latency_ms": float(self.config.submit_ack_latency_ms),
            "modify_ack_latency_ms": float(self.config.modify_ack_latency_ms),
            "cancel_ack_latency_ms": float(self.config.cancel_ack_latency_ms),
            "live_uplift_factor": float(self.config.live_uplift_factor),
            "model_applied": True,
            "engine": "hftbacktest_native",
            "backtest_engine": getattr(self.config, "backtest_engine", "hftbacktest_v2"),
            "queue_model": getattr(self.config, "queue_model", "PowerProbQueueModel(3.0)"),
            "latency_model": getattr(self.config, "latency_model", "ConstantLatency"),
            "exchange_model": getattr(self.config, "exchange_model", "NoPartialFillExchange"),
        }

        is_equities: list[np.ndarray] = []
        oos_equities: list[np.ndarray] = []
        is_signals_list: list[np.ndarray] = []
        oos_signals_list: list[np.ndarray] = []
        is_mid_list: list[np.ndarray] = []
        oos_mid_list: list[np.ndarray] = []
        is_pos_list: list[np.ndarray] = []
        oos_pos_list: list[np.ndarray] = []
        volumes: list[np.ndarray] = []

        tmp_paths: list[str] = []
        try:
            for data_path in self.config.data_paths:
                try:
                    hbt_path = ensure_hftbt_npz(data_path)
                except (FileNotFoundError, OSError):
                    continue  # file missing, skip

                is_path, oos_path = _split_npz(hbt_path, self.config.is_oos_split)
                tmp_paths.extend([is_path, oos_path])

                is_eq, is_sig, is_mid, is_pos = _run_adapter_slice(
                    self.alpha, is_path, self.config, self.symbol
                )
                oos_eq, oos_sig, oos_mid, oos_pos = _run_adapter_slice(
                    self.alpha, oos_path, self.config, self.symbol
                )

                is_equities.append(is_eq)
                oos_equities.append(oos_eq)
                is_signals_list.append(is_sig)
                oos_signals_list.append(oos_sig)
                is_mid_list.append(is_mid)
                oos_mid_list.append(oos_mid)
                is_pos_list.append(is_pos)
                oos_pos_list.append(oos_pos)
                # Volume not directly available from LOBStatsEvent; use ones
                volumes.append(np.ones(len(oos_sig), dtype=np.float64))

        finally:
            # Clean up temp NPZ files
            for p in tmp_paths:
                try:
                    os.unlink(p)
                    # Also try removing the temp dir if empty
                    d = os.path.dirname(p)
                    if os.path.isdir(d):
                        try:
                            os.rmdir(d)
                        except OSError:
                            pass
                except OSError:
                    pass

        empty = np.zeros(0, dtype=np.float64)
        run_id = str(uuid.uuid4())

        if not oos_equities:
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
                run_id=run_id,
                config_hash=_hash_config(self.config),
                latency_profile=latency_profile,
            )

        def _cat(arrays: list[np.ndarray]) -> np.ndarray:
            valid = [a for a in arrays if a.size > 0]
            if not valid:
                return empty
            return np.concatenate(valid) if len(valid) > 1 else valid[0]

        is_equity = _cat(is_equities)
        oos_equity = _cat(oos_equities)
        all_equity = _cat([is_equity, oos_equity])

        oos_signals = _cat(oos_signals_list)
        oos_mid = _cat(oos_mid_list)
        oos_pos = _cat(oos_pos_list)
        all_signals = _cat(is_signals_list + oos_signals_list)
        all_pos = _cat(is_pos_list + oos_pos_list)
        all_volume = _cat(volumes)

        oos_fwd_returns = _forward_returns(oos_mid)

        sharpe_is = compute_sharpe(is_equity) if is_equity.size >= 2 else 0.0
        sharpe_oos = compute_sharpe(oos_equity) if oos_equity.size >= 2 else 0.0
        ic_mean, ic_std, ic_series = compute_ic(oos_signals, oos_fwd_returns)
        ic_tstat, ic_pvalue = compute_ic_ttest(ic_series)
        ic_halflife = compute_ic_halflife(all_signals)
        turnover = compute_turnover(all_pos)
        max_dd = compute_max_drawdown(all_equity)
        sortino = compute_sortino(oos_equity) if oos_equity.size >= 2 else 0.0
        cvar_5 = compute_cvar(oos_equity, alpha=0.05) if oos_equity.size >= 2 else 0.0
        capacity = compute_capacity(all_pos, all_volume)
        regime = (
            _regime_metrics(oos_signals, oos_fwd_returns)
            if self.config.auto_regime_split
            else {}
        )

        return BacktestResult(
            signals=all_signals,
            equity_curve=all_equity,
            positions=all_pos,
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
            run_id=run_id,
            config_hash=_hash_config(self.config),
            latency_profile=latency_profile,
        )

    def run_walk_forward(
        self,
        alpha: AlphaProtocol,
        config: WalkForwardConfig | None = None,
    ) -> WalkForwardResult:
        """Walk-forward via repeated IS/OOS splits on each fold."""
        wf = config or WalkForwardConfig()

        # Collect all available hftbt.npz paths (auto-convert if needed)
        hbt_paths: list[str] = []
        for p in self.config.data_paths:
            try:
                hbt_paths.append(ensure_hftbt_npz(p))
            except (FileNotFoundError, OSError):
                pass

        if not hbt_paths:
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

        # Load all events to compute fold boundaries
        chunks = []
        for p in hbt_paths:
            d = np.load(p, allow_pickle=False)
            arr = np.asarray(d["data"]) if isinstance(d, np.lib.npyio.NpzFile) else np.asarray(d)
            chunks.append(arr)
        full = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        total_rows = len(full)

        n_splits = int(wf.n_splits)
        fold_size = total_rows // (n_splits + 1)
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

        folds: list[WalkForwardFoldResult] = []
        tmp_paths: list[str] = []
        try:
            for fold_idx in range(n_splits):
                train_end = fold_size * (fold_idx + 1)
                test_end = fold_size * (fold_idx + 2)
                train = full[:train_end]
                test = full[train_end:test_end]
                if len(train) < int(wf.min_train_samples) or len(test) < 2:
                    continue

                tmp_dir = tempfile.mkdtemp(prefix=f"hftnative_wf{fold_idx}_")
                test_path = os.path.join(tmp_dir, "test.npz")
                np.savez_compressed(test_path, data=test)
                tmp_paths.append(test_path)

                eq, sig, mid, pos = _run_adapter_slice(
                    alpha, test_path, self.config, self.symbol
                )
                fwd = _forward_returns(mid)
                sharpe = compute_sharpe(eq) if eq.size >= 2 else 0.0
                ic_mean, _, _ = compute_ic(sig, fwd)
                max_dd = compute_max_drawdown(eq)
                to = compute_turnover(pos)
                folds.append(WalkForwardFoldResult(
                    fold_idx=fold_idx,
                    train_size=len(train),
                    test_size=len(test),
                    sharpe=float(sharpe),
                    ic_mean=float(ic_mean),
                    max_drawdown=float(max_dd),
                    turnover=float(to),
                ))
        finally:
            for p in tmp_paths:
                try:
                    os.unlink(p)
                    os.rmdir(os.path.dirname(p))
                except OSError:
                    pass

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

        sharpes = np.asarray([f.sharpe for f in folds], dtype=np.float64)
        ics = np.asarray([f.ic_mean for f in folds], dtype=np.float64)
        consistency = float(np.mean(sharpes > 0.0))
        return WalkForwardResult(
            config=wf,
            folds=folds,
            fold_sharpe_mean=float(np.mean(sharpes)),
            fold_sharpe_std=float(np.std(sharpes)),
            fold_sharpe_min=float(np.min(sharpes)),
            fold_sharpe_max=float(np.max(sharpes)),
            fold_consistency_pct=consistency,
            fold_ic_mean=float(np.mean(ics)) if ics.size else float("nan"),
        )

    def run_regime_split(self) -> dict[str, float]:
        """Run OOS and compute Sharpe per vol regime (high/low)."""
        result = self.run()
        return result.regime_metrics


# ---------------------------------------------------------------------------
# Public helper for Gate C
# ---------------------------------------------------------------------------
def has_hftbt_data(data_paths: list[str]) -> bool:
    """Return True if at least one path has a sibling hftbt.npz."""
    return any(_resolve_hftbt_path(p) is not None for p in data_paths)


# ---------------------------------------------------------------------------
# CLI entrypoint (python -m research.backtest.hft_native_runner)
# ---------------------------------------------------------------------------
def _parse_args() -> "argparse.Namespace":
    import argparse
    parser = argparse.ArgumentParser(description="Run standardized research backtest via hftbacktest.")
    parser.add_argument("--alpha", required=True, help="alpha_id registered under research/alphas")
    parser.add_argument("--data", required=True, nargs="+", help="Path(s) to npy/npz data file(s)")
    parser.add_argument("--signal-threshold", type=float, default=0.3)
    parser.add_argument("--max-position", type=int, default=5)
    parser.add_argument("--is-oos-split", type=float, default=0.7)
    parser.add_argument("--out", default="", help="Optional JSON output path for summary metrics")
    return parser.parse_args()


def main() -> int:
    import json
    from pathlib import Path

    from research.backtest.types import BacktestConfig
    from research.registry.alpha_registry import AlphaRegistry

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
