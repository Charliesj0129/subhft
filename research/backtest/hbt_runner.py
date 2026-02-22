from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from research.backtest.metrics import (
    compute_capacity,
    compute_ic,
    compute_max_drawdown,
    compute_sharpe,
    compute_turnover,
)
from research.registry.alpha_registry import AlphaRegistry
from research.registry.schemas import AlphaProtocol


@dataclass(frozen=True)
class BacktestConfig:
    data_paths: list[str]
    is_oos_split: float = 0.7
    latency_ns: int = 1_000_000
    maker_fee_bps: float = -0.2
    taker_fee_bps: float = 0.2
    signal_threshold: float = 0.3
    max_position: int = 5
    initial_equity: float = 1_000_000.0


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
    turnover: float
    max_drawdown: float
    regime_metrics: dict[str, float]
    capacity_estimate: float
    run_id: str
    config_hash: str


class ResearchBacktestRunner:
    def __init__(self, alpha: AlphaProtocol, config: BacktestConfig):
        self.alpha = alpha
        self.config = config

    def run(self) -> BacktestResult:
        data = self._load_data(self.config.data_paths[0])
        price = self._extract_price(data)
        volume = self._extract_volume(data, len(price))
        signals = self._generate_signals(data, len(price))
        positions = self._signals_to_positions(signals)
        equity = self._compute_equity_curve(price, positions)
        fwd_returns = self._forward_returns(price)

        split = max(2, int(len(equity) * self.config.is_oos_split))
        split = min(split, len(equity) - 1) if len(equity) > 2 else len(equity)

        sharpe_is = compute_sharpe(equity[:split]) if split >= 2 else 0.0
        sharpe_oos = compute_sharpe(equity[split - 1 :]) if split >= 2 else 0.0
        ic_mean, ic_std, ic_series = compute_ic(signals, fwd_returns)
        turnover = compute_turnover(positions)
        max_dd = compute_max_drawdown(equity)
        capacity = compute_capacity(positions, volume)
        regime = self._regime_metrics(fwd_returns, positions)

        return BacktestResult(
            signals=signals,
            equity_curve=equity,
            positions=positions,
            sharpe_is=sharpe_is,
            sharpe_oos=sharpe_oos,
            ic_series=ic_series,
            ic_mean=ic_mean,
            ic_std=ic_std,
            turnover=turnover,
            max_drawdown=max_dd,
            regime_metrics=regime,
            capacity_estimate=capacity,
            run_id=str(uuid.uuid4()),
            config_hash=_hash_config(self.config),
        )

    def run_regime_split(self) -> dict[str, BacktestResult]:
        base = self.run()
        if base.signals.size < 16:
            return {"all": base}

        returns = self._forward_returns(self._extract_price(self._load_data(self.config.data_paths[0])))
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
            for field in ("mid", "mid_price", "px", "price", "close"):
                if field in data.dtype.names:
                    return np.asarray(data[field], dtype=np.float64)
            if "best_bid" in data.dtype.names and "best_ask" in data.dtype.names:
                bid = np.asarray(data["best_bid"], dtype=np.float64)
                ask = np.asarray(data["best_ask"], dtype=np.float64)
                return (bid + ask) / 2.0
        return np.asarray(data, dtype=np.float64).reshape(-1)

    def _extract_volume(self, data: np.ndarray, n: int) -> np.ndarray:
        if data.dtype.names and "qty" in data.dtype.names:
            return np.asarray(data["qty"], dtype=np.float64)[:n]
        return np.ones(n, dtype=np.float64)

    def _generate_signals(self, data: np.ndarray, n: int) -> np.ndarray:
        self.alpha.reset()
        signals = np.zeros(n, dtype=np.float64)
        if data.dtype.names:
            field_names = data.dtype.names
            for i in range(n):
                row = data[i]
                payload = {name: _to_python_scalar(row[name]) for name in field_names}
                payload = _with_standard_aliases(payload)
                signals[i] = float(self.alpha.update(**payload))
            return signals

        flat = np.asarray(data, dtype=np.float64).reshape(-1)
        for i, value in enumerate(flat):
            signals[i] = float(self.alpha.update(value=value))
        return signals

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

    def _compute_equity_curve(self, price: np.ndarray, positions: np.ndarray) -> np.ndarray:
        n = min(price.size, positions.size)
        if n < 2:
            return np.asarray([self.config.initial_equity], dtype=np.float64)
        px = price[:n]
        pos = positions[:n]

        pnl_step = pos[:-1] * np.diff(px)
        turnover = np.abs(np.diff(pos, prepend=0))
        fee_rate = max(self.config.taker_fee_bps, 0.0) / 10_000.0
        fee_step = turnover[1:] * np.abs(px[1:]) * fee_rate
        pnl_after_fee = pnl_step - fee_step
        pnl_cum = np.cumsum(pnl_after_fee, dtype=np.float64)

        equity = np.empty(n, dtype=np.float64)
        equity[0] = float(self.config.initial_equity)
        equity[1:] = self.config.initial_equity + pnl_cum
        return equity

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
        return BacktestResult(
            signals=sig,
            equity_curve=eq,
            positions=pos,
            sharpe_is=sharpe,
            sharpe_oos=sharpe,
            ic_series=ic_series,
            ic_mean=ic_mean,
            ic_std=ic_std,
            turnover=compute_turnover(pos),
            max_drawdown=compute_max_drawdown(eq),
            regime_metrics={},
            capacity_estimate=base.capacity_estimate,
            run_id=base.run_id,
            config_hash=base.config_hash,
        )


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


def _with_standard_aliases(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    if "bid_px" not in out:
        out["bid_px"] = out.get("best_bid", out.get("bid_price", out.get("bid")))
    if "ask_px" not in out:
        out["ask_px"] = out.get("best_ask", out.get("ask_price", out.get("ask")))
    if "bid_qty" not in out:
        out["bid_qty"] = out.get("bid_depth", out.get("bid_size", out.get("bqty", 0.0)))
    if "ask_qty" not in out:
        out["ask_qty"] = out.get("ask_depth", out.get("ask_size", out.get("aqty", 0.0)))
    if "trade_vol" not in out:
        out["trade_vol"] = out.get("qty", out.get("volume", out.get("trade_qty", 0.0)))
    if "current_mid" not in out:
        if out.get("bid_px") is not None and out.get("ask_px") is not None:
            out["current_mid"] = (float(out["bid_px"]) + float(out["ask_px"])) / 2.0
        else:
            out["current_mid"] = out.get("mid", out.get("mid_price", out.get("price", 0.0)))
    return out


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
    result = ResearchBacktestRunner(alpha, config).run()
    summary = {
        "alpha_id": alpha.manifest.alpha_id,
        "run_id": result.run_id,
        "config_hash": result.config_hash,
        "sharpe_is": result.sharpe_is,
        "sharpe_oos": result.sharpe_oos,
        "ic_mean": result.ic_mean,
        "ic_std": result.ic_std,
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
