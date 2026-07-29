"""Stateful Pine-compatible SMMA features for governed alpha mining.

SMMA is deliberately computed before the finite-window GP expression layer.
The recursive state is causal and O(1); the GP only sees stationary derived
features (distance, slope, spread, alignment), never a raw price or SMMA level.
"""

from __future__ import annotations

import itertools
import re
from collections import deque
from typing import Any, Mapping, Sequence

import numpy as np

SMMA_SOURCES: tuple[str, ...] = ("close", "hl2", "hlc3", "ohlc4")
SMMA_LENGTHS: tuple[int, ...] = (3, 5, 7, 10, 14, 21, 34, 55)
FIBONACCI_SMMA_LENGTHS: tuple[int, ...] = (1, 2, 3, 5, 8, 13, 21, 34, 55)
SMMA_SLOPE_LAGS: tuple[int, ...] = (1, 3, 6)
SMMA_NORMALIZERS: tuple[str, ...] = ("atr14", "retvol14")
_FEATURE_LENGTHS_RE = re.compile(r"^(?:close|hl2|hlc3|ohlc4)_l(?P<lengths>[0-9_]+)_(?:atr14|retvol14)_")


class SMMAState:
    """Incremental Pine ``ta.rma``/SMMA state.

    The first ``length`` finite observations seed with their arithmetic mean.
    A non-finite observation emits NaN and does not advance the state. Call
    :meth:`reset` at a roll, data hole, or discontinuous segment.
    """

    __slots__ = ("length", "_seed_count", "_seed_sum", "_value")

    def __init__(self, length: int) -> None:
        if int(length) < 1:
            raise ValueError("SMMA length must be >= 1")
        self.length = int(length)
        self._seed_count = 0
        self._seed_sum = 0.0
        self._value = np.nan

    @property
    def value(self) -> float:
        return float(self._value)

    def update(self, raw: float) -> float:
        value = float(raw)
        if not np.isfinite(value):
            return float("nan")
        if self.length == 1:
            self._seed_count = 1
            self._seed_sum = value
            self._value = value
            return value
        if self._seed_count < self.length:
            self._seed_count += 1
            self._seed_sum += value
            if self._seed_count == self.length:
                self._value = self._seed_sum / float(self.length)
            return float(self._value)
        self._value = ((self._value * float(self.length - 1)) + value) / float(self.length)
        return float(self._value)

    def reset(self) -> None:
        self._seed_count = 0
        self._seed_sum = 0.0
        self._value = np.nan

    def snapshot(self) -> dict[str, float | int]:
        return {
            "length": self.length,
            "seed_count": self._seed_count,
            "seed_sum": self._seed_sum,
            "value": float(self._value),
        }

    def restore(self, payload: Mapping[str, Any]) -> None:
        if int(payload.get("length", -1)) != self.length:
            raise ValueError("SMMA checkpoint length mismatch")
        seed_count = int(payload.get("seed_count", -1))
        if seed_count < 0 or seed_count > self.length:
            raise ValueError("SMMA checkpoint seed_count is invalid")
        self._seed_count = seed_count
        self._seed_sum = float(payload.get("seed_sum", 0.0))
        self._value = float(payload.get("value", np.nan))


def pine_smma(
    values: Sequence[float] | np.ndarray,
    length: int,
    *,
    reset_mask: Sequence[bool] | np.ndarray | None = None,
) -> np.ndarray:
    """Return Pine-compatible SMMA values with explicit causal resets."""
    source = np.asarray(values, dtype=np.float64).reshape(-1)
    resets = (
        np.zeros(source.size, dtype=np.bool_)
        if reset_mask is None
        else np.asarray(reset_mask, dtype=np.bool_).reshape(-1)
    )
    if resets.size != source.size:
        raise ValueError("reset_mask length must match values")
    state = SMMAState(length)
    out = np.full(source.size, np.nan, dtype=np.float64)
    for index, value in enumerate(source):
        if resets[index]:
            state.reset()
        out[index] = state.update(float(value))
    return out


def source_values(
    open_: Sequence[float] | np.ndarray,
    high: Sequence[float] | np.ndarray,
    low: Sequence[float] | np.ndarray,
    close: Sequence[float] | np.ndarray,
) -> dict[str, np.ndarray]:
    """Build the four fixed SMMA source series."""
    arrays = [np.asarray(values, dtype=np.float64).reshape(-1) for values in (open_, high, low, close)]
    if len({array.size for array in arrays}) != 1:
        raise ValueError("OHLC arrays must have identical lengths")
    open_arr, high_arr, low_arr, close_arr = arrays
    return {
        "close": close_arr,
        "hl2": (high_arr + low_arr) / 2.0,
        "hlc3": (high_arr + low_arr + close_arr) / 3.0,
        "ohlc4": (open_arr + high_arr + low_arr + close_arr) / 4.0,
    }


def segmented_atr14(
    high: Sequence[float] | np.ndarray,
    low: Sequence[float] | np.ndarray,
    close: Sequence[float] | np.ndarray,
    reset_mask: Sequence[bool] | np.ndarray,
) -> np.ndarray:
    """Pine-style ATR(14), reset at every non-contiguous segment."""
    high_arr = np.asarray(high, dtype=np.float64).reshape(-1)
    low_arr = np.asarray(low, dtype=np.float64).reshape(-1)
    close_arr = np.asarray(close, dtype=np.float64).reshape(-1)
    resets = np.asarray(reset_mask, dtype=np.bool_).reshape(-1)
    if len({high_arr.size, low_arr.size, close_arr.size, resets.size}) != 1:
        raise ValueError("ATR inputs must have identical lengths")
    true_range = np.full(close_arr.size, np.nan, dtype=np.float64)
    previous_close = np.nan
    for index in range(close_arr.size):
        if resets[index]:
            previous_close = np.nan
        if not (np.isfinite(high_arr[index]) and np.isfinite(low_arr[index])):
            continue
        intrabar = high_arr[index] - low_arr[index]
        if np.isfinite(previous_close):
            true_range[index] = max(
                intrabar,
                abs(high_arr[index] - previous_close),
                abs(low_arr[index] - previous_close),
            )
        else:
            true_range[index] = intrabar
        if np.isfinite(close_arr[index]):
            previous_close = close_arr[index]
    return pine_smma(true_range, 14, reset_mask=resets)


def segmented_return_vol14(
    close: Sequence[float] | np.ndarray,
    reset_mask: Sequence[bool] | np.ndarray,
) -> np.ndarray:
    """Rolling 14-bar standard deviation of simple returns, without gap fill."""
    close_arr = np.asarray(close, dtype=np.float64).reshape(-1)
    resets = np.asarray(reset_mask, dtype=np.bool_).reshape(-1)
    if close_arr.size != resets.size:
        raise ValueError("return-vol inputs must have identical lengths")
    out = np.full(close_arr.size, np.nan, dtype=np.float64)
    window: deque[float] = deque(maxlen=14)
    previous = np.nan
    for index, current in enumerate(close_arr):
        if resets[index]:
            window.clear()
            previous = np.nan
        if not np.isfinite(current):
            continue
        if np.isfinite(previous) and abs(previous) > 1e-12:
            window.append((current / previous) - 1.0)
            if len(window) == 14:
                out[index] = float(np.std(np.asarray(window, dtype=np.float64)))
        previous = current
    return out


def _safe_normalize(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    out: np.ndarray = np.full(numerator.size, np.nan, dtype=np.float64)
    valid = np.isfinite(numerator) & np.isfinite(denominator) & (np.abs(denominator) > 1e-12)
    np.divide(numerator, denominator, out=out, where=valid)
    return out


def _segmented_lag_delta(values: np.ndarray, lag: int, reset_mask: np.ndarray) -> np.ndarray:
    out: np.ndarray = np.full(values.size, np.nan, dtype=np.float64)
    segment_start = 0
    for index in range(values.size):
        if reset_mask[index]:
            segment_start = index
        previous_index = index - lag
        if previous_index < segment_start:
            continue
        if np.isfinite(values[index]) and np.isfinite(values[previous_index]):
            out[index] = values[index] - values[previous_index]
    return out


def normalize_smma_lengths(lengths: Sequence[int]) -> tuple[int, ...]:
    """Return a non-empty, strictly increasing SMMA length specification."""
    normalized = tuple(int(value) for value in lengths)
    if not normalized:
        raise ValueError("at least one SMMA length is required")
    if any(value < 1 for value in normalized):
        raise ValueError("SMMA lengths must be >= 1")
    if tuple(sorted(set(normalized))) != normalized:
        raise ValueError("SMMA lengths must be unique and strictly increasing")
    return normalized


def smma_lengths_from_expression(expression: str) -> tuple[int, ...]:
    """Recover the exact recursive lengths encoded by family feature names."""
    from research.combinatorial.expression_lang import compile_expression

    compiled = compile_expression(expression, max_depth=3)
    lengths: set[int] = set()
    for name in compiled.variables:
        match = _FEATURE_LENGTHS_RE.match(name)
        if match is None:
            raise ValueError(f"not an SMMA family feature: {name}")
        lengths.update(int(value) for value in match.group("lengths").split("_"))
    return normalize_smma_lengths(sorted(lengths))


def build_smma_family_features(
    *,
    open_: Sequence[float] | np.ndarray,
    high: Sequence[float] | np.ndarray,
    low: Sequence[float] | np.ndarray,
    close: Sequence[float] | np.ndarray,
    reset_mask: Sequence[bool] | np.ndarray,
    lengths: Sequence[int] = SMMA_LENGTHS,
) -> dict[str, np.ndarray]:
    """Compute the fixed SMMA family, exposing stationary features only."""
    smma_lengths = normalize_smma_lengths(lengths)
    resets = np.asarray(reset_mask, dtype=np.bool_).reshape(-1)
    sources = source_values(open_, high, low, close)
    close_arr = sources["close"]
    if close_arr.size != resets.size:
        raise ValueError("reset_mask length must match OHLC")
    normalizer_resets = np.zeros_like(resets)
    normalizers = {
        "atr14": segmented_atr14(high, low, close_arr, normalizer_resets),
        "retvol14": segmented_return_vol14(close_arr, normalizer_resets) * np.abs(close_arr),
    }
    features: dict[str, np.ndarray] = {}
    for source_name, source in sources.items():
        levels = {length: pine_smma(source, length, reset_mask=resets) for length in smma_lengths}
        for norm_name, normalizer in normalizers.items():
            for length, level in levels.items():
                prefix = f"{source_name}_l{length}_{norm_name}"
                features[f"{prefix}_distance"] = _safe_normalize(source - level, normalizer)
                for lag in SMMA_SLOPE_LAGS:
                    delta = _segmented_lag_delta(level, lag, resets)
                    features[f"{prefix}_slope{lag}"] = _safe_normalize(delta, normalizer)
            for fast, slow in itertools.combinations(smma_lengths, 2):
                spread = levels[fast] - levels[slow]
                prefix = f"{source_name}_l{fast}_{slow}_{norm_name}"
                features[f"{prefix}_spread"] = _safe_normalize(spread, normalizer)
                state = np.sign(spread)
                state[~np.isfinite(spread)] = np.nan
                features[f"{prefix}_cross_state"] = state
                spread_delta = _segmented_lag_delta(spread, 1, resets)
                features[f"{prefix}_spread_delta"] = _safe_normalize(spread_delta, normalizer)
            for fast, middle, slow in itertools.combinations(smma_lengths, 3):
                fast_level, middle_level, slow_level = levels[fast], levels[middle], levels[slow]
                valid = np.isfinite(fast_level) & np.isfinite(middle_level) & np.isfinite(slow_level)
                alignment: np.ndarray = np.full(source.size, np.nan, dtype=np.float64)
                alignment[valid] = 0.0
                alignment[valid & (fast_level > middle_level) & (middle_level > slow_level)] = 1.0
                alignment[valid & (fast_level < middle_level) & (middle_level < slow_level)] = -1.0
                separation = np.abs(fast_level - middle_level) + np.abs(middle_level - slow_level)
                prefix = f"{source_name}_l{fast}_{middle}_{slow}_{norm_name}"
                features[f"{prefix}_alignment"] = alignment
                features[f"{prefix}_separation"] = _safe_normalize(separation, normalizer)
    return features


def evaluate_smma_expression(
    expression: str,
    features: Mapping[str, np.ndarray],
    reset_mask: Sequence[bool] | np.ndarray,
) -> np.ndarray:
    """Evaluate the GP tail without allowing rolling windows to cross SMMA resets."""
    from research.combinatorial.expression_eval import evaluate_family_expression

    try:
        return evaluate_family_expression(expression, features, reset_mask)
    except ValueError as exc:
        message = str(exc)
        if message == "expression features and reset mask must have identical lengths":
            raise ValueError("SMMA expression features and reset mask must have identical lengths") from exc
        if message == "expressions must reference at least one family feature":
            raise ValueError("SMMA expressions must reference at least one family feature") from exc
        raise


def validate_stationary_signal(values: Sequence[float] | np.ndarray) -> tuple[bool, str]:
    """Allow causal warm-up NaNs but reject infinities and unusable finite output."""
    signal = np.asarray(values, dtype=np.float64).reshape(-1)
    if np.any(np.isinf(signal)):
        return False, "non_finite_output"
    finite = signal[np.isfinite(signal)]
    if finite.size == 0:
        return False, "non_finite_output"
    if float(np.max(finite) - np.min(finite)) <= 1e-12:
        return False, "constant_signal"
    return True, ""


def generated_gp_expressions(
    feature_names: Sequence[str],
    *,
    seed: int,
    limit: int,
) -> list[str]:
    """Generate deterministic depth<=3 GP expressions with invalid forms banned."""
    names = sorted(set(str(name) for name in feature_names))
    rng = np.random.default_rng(int(seed))
    if not names or limit <= 0:
        return []
    order = rng.permutation(len(names))
    expressions: list[str] = []
    windows = (3, 5, 7, 14)
    seen: set[str] = set()
    attempts = 0
    while len(expressions) < limit and attempts < max(limit * 20, 100):
        slot = attempts % 7
        index = int(order[(attempts // 7) % len(order)])
        name = names[index]
        if slot == 0:
            expression = name
        elif slot == 1:
            expression = f"sign({name})"
        elif slot == 2:
            expression = f"ts_delta({name}, {windows[index % len(windows)]})"
        elif slot == 3:
            expression = f"zscore({name}, {windows[(index + 1) % len(windows)]})"
        else:
            if len(names) < 2:
                attempts += 1
                continue
            left_index, right_index = rng.choice(len(names), size=2, replace=False)
            left, right = names[int(left_index)], names[int(right_index)]
            operator = ("add", "mul", "ts_corr")[slot - 4]
            expression = (
                f"ts_corr({left}, {right}, {windows[int(rng.integers(0, len(windows)))]})"
                if operator == "ts_corr"
                else f"{operator}({left}, {right})"
            )
        if expression not in seen:
            seen.add(expression)
            expressions.append(expression)
        attempts += 1
    return expressions
