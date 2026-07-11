"""Causal reconstruction of Zeiierman's Neural Weight Oscillator.

The public description discloses the three component formulas, BWM relation,
defaults, and alert semantics. TradingView does not expose plain Pine source in
the page payload, so this module intentionally identifies itself as a disclosed-
formula reconstruction rather than a 1:1 source port.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class IndicatorConfig:
    fast_len: int = 20
    slow_len: int = 100
    smoothing_len: int = 5
    signal_len: int = 9
    rsi_len: int = 14
    mean_len: int = 30
    momentum_len: int = 14
    atr_len: int = 14
    target_len: int = 5
    influence: float = 0.30
    line_impact: float = 0.25
    learning_rate: float = 0.02


@dataclass(frozen=True)
class IndicatorResult:
    oscillator: np.ndarray
    signal: np.ndarray
    trigger_long: np.ndarray
    trigger_short: np.ndarray
    component_scores: np.ndarray
    learned_weights: np.ndarray
    learning_updates: np.ndarray
    regime: np.ndarray


def compute_bwm_weights(
    *,
    best_to_others: tuple[float, float, float] = (1.0, 3.0, 6.0),
    others_to_worst: tuple[float, float, float] = (6.0, 3.0, 1.0),
) -> np.ndarray:
    """Return normalized BWM relation weights for Trend, Mean, Momentum."""
    bo = np.asarray(best_to_others, dtype=float)
    ow = np.asarray(others_to_worst, dtype=float)
    if bo.shape != (3,) or ow.shape != (3,) or np.any(bo <= 0) or np.any(ow <= 0):
        raise ValueError("BWM comparison vectors must contain three positive values")
    best_to_worst = float(np.max(bo))
    relative = np.sqrt((best_to_worst / bo) * ow)
    weights = relative / relative.sum()
    weights[-1] = 1.0 - float(weights[:-1].sum())
    return weights


def cross_signals(oscillator: np.ndarray, signal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Apply the published alert rules without reading future bars."""
    oscillator = np.asarray(oscillator, dtype=float)
    signal = np.asarray(signal, dtype=float)
    if oscillator.shape != signal.shape:
        raise ValueError("oscillator and signal must have identical shapes")
    long_signal = np.zeros(oscillator.shape, dtype=bool)
    short_signal = np.zeros(oscillator.shape, dtype=bool)
    if oscillator.size < 2:
        return long_signal, short_signal
    valid = (
        np.isfinite(oscillator[1:])
        & np.isfinite(signal[1:])
        & np.isfinite(oscillator[:-1])
        & np.isfinite(signal[:-1])
    )
    long_signal[1:] = (
        valid
        & (oscillator[:-1] <= signal[:-1])
        & (oscillator[1:] > signal[1:])
        & (oscillator[1:] > 50.0)
    )
    short_signal[1:] = (
        valid
        & (oscillator[:-1] >= signal[:-1])
        & (oscillator[1:] < signal[1:])
        & (oscillator[1:] < 50.0)
    )
    return long_signal, short_signal


def _ema(values: np.ndarray, length: int) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=float)
    if length <= 0:
        raise ValueError("EMA length must be positive")
    alpha = 2.0 / (length + 1.0)
    start = None
    for i, value in enumerate(values):
        if not np.isfinite(value):
            continue
        if start is None:
            start = i
            out[i] = value
        else:
            out[i] = alpha * value + (1.0 - alpha) * out[i - 1]
    return out


def _rma(values: np.ndarray, length: int) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=float)
    if len(values) < length:
        return out
    seed = values[:length]
    if not np.all(np.isfinite(seed)):
        return out
    out[length - 1] = float(np.mean(seed))
    for i in range(length, len(values)):
        out[i] = (out[i - 1] * (length - 1) + values[i]) / length
    return out


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, length: int) -> np.ndarray:
    previous = np.r_[close[0], close[:-1]]
    true_range = np.maximum(high - low, np.maximum(np.abs(high - previous), np.abs(low - previous)))
    return _rma(true_range, length)


def _rsi(close: np.ndarray, length: int) -> np.ndarray:
    delta = np.diff(close, prepend=close[0])
    gains = _rma(np.maximum(delta, 0.0), length)
    losses = _rma(np.maximum(-delta, 0.0), length)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = gains / losses
        rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi[(losses == 0) & (gains > 0)] = 100.0
    rsi[(losses == 0) & (gains == 0)] = 50.0
    return rsi


def _rolling_mean(values: np.ndarray, length: int) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=float)
    for i in range(length - 1, len(values)):
        window = values[i - length + 1 : i + 1]
        if np.all(np.isfinite(window)):
            out[i] = float(np.mean(window))
    return out


def _rolling_std(values: np.ndarray, length: int) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=float)
    for i in range(length - 1, len(values)):
        window = values[i - length + 1 : i + 1]
        if np.all(np.isfinite(window)):
            out[i] = float(np.std(window))
    return out


def _normalize(values: np.ndarray, low: float, high: float) -> np.ndarray:
    return np.clip((values - low) / (high - low), 0.0, 1.0) * 100.0


def _component_scores(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    config: IndicatorConfig,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    ema_fast = _ema(close, config.fast_len)
    ema_slow = _ema(close, config.slow_len)
    atr = _atr(high, low, close, config.atr_len)
    rsi = _rsi(close, config.rsi_len)
    basis = _rolling_mean(close, config.mean_len)
    deviation = _rolling_std(close, config.mean_len)

    with np.errstate(divide="ignore", invalid="ignore"):
        spread = (ema_fast - ema_slow) / atr
        slope = (ema_fast - np.r_[np.nan, ema_fast[:-1]]) / atr
        z_score = np.where(deviation == 0.0, 0.0, (close - basis) / deviation)
        roc = close / np.r_[np.full(config.momentum_len, np.nan), close[:-config.momentum_len]] - 1.0

    trend = _normalize(spread + slope, -2.5, 2.5)
    mean = (100.0 - rsi) * 0.5 + _normalize(-z_score, -2.5, 2.5) * 0.5
    roc_norm = _normalize(roc, -0.05, 0.05)
    ema_momentum = _normalize(slope, -1.0, 1.0)
    momentum = roc_norm * 0.45 + rsi * 0.35 + ema_momentum * 0.20
    scores = np.column_stack((trend, mean, momentum))
    return scores, atr, spread, slope


def _classify_regime(atr: np.ndarray, spread: np.ndarray, slope: np.ndarray) -> np.ndarray:
    regime = np.full(len(atr), "unavailable", dtype=object)
    for i in range(len(atr)):
        if not np.isfinite(atr[i]) or not np.isfinite(spread[i]) or not np.isfinite(slope[i]):
            continue
        history = atr[:i]
        history = history[np.isfinite(history)]
        high_vol = history.size >= 30 and atr[i] > np.quantile(history, 0.75)
        if high_vol:
            regime[i] = "high_vol"
        elif abs(spread[i]) >= 0.35 and abs(slope[i]) >= 0.02:
            regime[i] = "trend"
        else:
            regime[i] = "range"
    return regime


def compute_indicator(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    *,
    config: IndicatorConfig | None = None,
) -> IndicatorResult:
    """Compute oscillator outputs using only information available at each close."""
    del open_  # The disclosed formula uses close-derived components.
    config = config or IndicatorConfig()
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    if not (high.shape == low.shape == close.shape):
        raise ValueError("OHLC arrays must have identical shapes")

    scores, atr, spread, slope = _component_scores(high, low, close, config)
    base_weights = compute_bwm_weights()
    learned = np.zeros(3, dtype=float)
    bias = 0.0
    raw = np.full(close.shape, np.nan, dtype=float)
    learned_history = np.zeros((len(close), 3), dtype=float)
    update_history = np.zeros(len(close), dtype=int)
    update_count = 0

    for i in range(len(close)):
        sample_i = i - config.target_len
        if sample_i >= 0 and np.all(np.isfinite(scores[sample_i])) and np.isfinite(atr[sample_i]):
            target_return = close[i] / close[sample_i] - 1.0
            target = float(np.sign(target_return))
            if target != 0.0:
                sample = (scores[sample_i] - 50.0) / 50.0
                prediction = float(np.tanh(np.dot(learned, sample) + bias))
                vol = max(float(atr[sample_i] / close[sample_i]), 1e-6)
                quality = min(abs(target_return) / vol, 2.0)
                error = prediction - target
                learned -= config.learning_rate * quality * error * sample
                bias -= config.learning_rate * quality * error
                learned = np.clip(learned, -1.5, 1.5)
                bias = float(np.clip(bias, -1.5, 1.5))
                update_count += 1

        learned_history[i] = learned
        update_history[i] = update_count
        if not np.all(np.isfinite(scores[i])):
            continue
        amplifiers = np.clip(1.0 + learned * config.influence, 0.25, 1.75)
        adaptive_weights = base_weights * amplifiers
        adaptive_weights /= adaptive_weights.sum()
        structural = float(np.dot(adaptive_weights, scores[i]))
        current = (scores[i] - 50.0) / 50.0
        learned_line = 50.0 + 50.0 * np.tanh(np.dot(learned, current) + bias)
        raw[i] = (1.0 - config.line_impact) * structural + config.line_impact * learned_line

    oscillator = _ema(raw, config.smoothing_len)
    signal = _ema(oscillator, config.signal_len)
    trigger_long, trigger_short = cross_signals(oscillator, signal)
    return IndicatorResult(
        oscillator=oscillator,
        signal=signal,
        trigger_long=trigger_long,
        trigger_short=trigger_short,
        component_scores=scores,
        learned_weights=learned_history,
        learning_updates=update_history,
        regime=_classify_regime(atr, spread, slope),
    )
