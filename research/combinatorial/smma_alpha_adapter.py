"""Streaming ``AlphaProtocol`` adapter for promoted SMMA-family candidates."""

from __future__ import annotations

import re
from collections import deque
from typing import Any, Mapping

import numpy as np

from hft_platform.contracts.alpha import AlphaManifest
from research.combinatorial.gp_alpha_adapter import GPCompiledAlpha
from research.combinatorial.smma import SMMAState

_FEATURE_RE = re.compile(
    r"^(?P<source>close|hl2|hlc3|ohlc4)_l(?P<lengths>[0-9_]+)_"
    r"(?P<normalizer>atr14|retvol14)_(?P<kind>distance|slope[136]|spread|cross_state|spread_delta|"
    r"alignment|separation)$"
)


class _StreamingFamilyFeatures:
    def __init__(self, feature_names: tuple[str, ...]) -> None:
        self._specs: dict[str, tuple[str, tuple[int, ...], str, str]] = {}
        required_sources: dict[str, set[int]] = {}
        for name in feature_names:
            match = _FEATURE_RE.fullmatch(name)
            if match is None:
                raise ValueError(f"not an SMMA derived feature: {name}")
            lengths = tuple(int(value) for value in match.group("lengths").split("_"))
            kind = match.group("kind")
            expected = 1 if kind in {"distance", "slope1", "slope3", "slope6"} else 2
            if kind in {"alignment", "separation"}:
                expected = 3
            if len(lengths) != expected or tuple(sorted(lengths)) != lengths:
                raise ValueError(f"invalid SMMA feature lengths for {name}")
            source = match.group("source")
            self._specs[name] = (source, lengths, match.group("normalizer"), kind)
            required_sources.setdefault(source, set()).update(lengths)
        self._states = {
            (source, length): SMMAState(length) for source, lengths in required_sources.items() for length in lengths
        }
        self._histories: dict[tuple[str, int], deque[float]] = {key: deque(maxlen=7) for key in self._states}
        self._previous_spread: dict[tuple[str, int, int], float] = {}
        self._atr = SMMAState(14)
        self._previous_close = np.nan
        self._return_window: deque[float] = deque(maxlen=14)

    def reset(self) -> None:
        self.reset_segment()
        self._atr.reset()
        self._previous_close = np.nan
        self._return_window.clear()

    def reset_segment(self) -> None:
        for state in self._states.values():
            state.reset()
        for history in self._histories.values():
            history.clear()
        self._previous_spread.clear()

    def update(self, *, open_: float, high: float, low: float, close: float) -> dict[str, float]:
        sources = {
            "close": close,
            "hl2": (high + low) / 2.0,
            "hlc3": (high + low + close) / 3.0,
            "ohlc4": (open_ + high + low + close) / 4.0,
        }
        if np.isfinite(self._previous_close):
            true_range = max(
                high - low,
                abs(high - self._previous_close),
                abs(low - self._previous_close),
            )
            if abs(self._previous_close) > 1e-12:
                self._return_window.append((close / self._previous_close) - 1.0)
        else:
            true_range = high - low
        atr = self._atr.update(true_range)
        retvol = (
            float(np.std(np.asarray(self._return_window, dtype=np.float64))) * abs(close)
            if len(self._return_window) == 14
            else np.nan
        )
        self._previous_close = close
        levels: dict[tuple[str, int], float] = {}
        for key, state in self._states.items():
            source, _length = key
            value = state.update(sources[source])
            levels[key] = value
            self._histories[key].append(value)
        normalizers = {"atr14": atr, "retvol14": retvol}
        out: dict[str, float] = {}
        current_spreads: dict[tuple[str, int, int], float] = {}
        for name, (source, lengths, normalizer_name, kind) in self._specs.items():
            normalizer = normalizers[normalizer_name]
            values = tuple(levels[(source, length)] for length in lengths)
            out[name] = self._feature_value(
                source=source,
                lengths=lengths,
                normalizer=normalizer,
                kind=kind,
                source_value=sources[source],
                values=values,
                current_spreads=current_spreads,
            )
        self._previous_spread.update(current_spreads)
        return out

    def _feature_value(
        self,
        *,
        source: str,
        lengths: tuple[int, ...],
        normalizer: float,
        kind: str,
        source_value: float,
        values: tuple[float, ...],
        current_spreads: dict[tuple[str, int, int], float],
    ) -> float:
        if not all(np.isfinite(value) for value in values):
            return np.nan
        if kind == "cross_state":
            return float(np.sign(values[0] - values[1]))
        if kind == "alignment":
            if values[0] > values[1] > values[2]:
                return 1.0
            if values[0] < values[1] < values[2]:
                return -1.0
            return 0.0
        if not np.isfinite(normalizer) or abs(normalizer) <= 1e-12:
            return np.nan
        if kind == "distance":
            return (source_value - values[0]) / normalizer
        if kind.startswith("slope"):
            lag = int(kind[-1])
            history = self._histories[(source, lengths[0])]
            prior = history[-lag - 1] if len(history) > lag else np.nan
            return (values[0] - prior) / normalizer if np.isfinite(prior) else np.nan
        if kind == "spread":
            return (values[0] - values[1]) / normalizer
        if kind == "spread_delta":
            key = (source, lengths[0], lengths[1])
            spread = values[0] - values[1]
            current_spreads[key] = spread
            prior = self._previous_spread.get(key, np.nan)
            return (spread - prior) / normalizer if np.isfinite(prior) else np.nan
        if kind == "separation":
            return (abs(values[0] - values[1]) + abs(values[1] - values[2])) / normalizer
        raise ValueError(f"unknown SMMA feature kind: {kind}")  # pragma: no cover

    def snapshot(self) -> dict[str, Any]:
        return {
            "states": {f"{source}:{length}": state.snapshot() for (source, length), state in self._states.items()},
            "histories": {f"{source}:{length}": list(history) for (source, length), history in self._histories.items()},
            "previous_spread": {
                f"{source}:{fast}:{slow}": value for (source, fast, slow), value in self._previous_spread.items()
            },
            "atr": self._atr.snapshot(),
            "previous_close": self._previous_close,
            "return_window": list(self._return_window),
        }

    def restore(self, payload: Mapping[str, Any]) -> None:
        states = payload.get("states")
        histories = payload.get("histories")
        if not isinstance(states, Mapping) or not isinstance(histories, Mapping):
            raise ValueError("SMMA feature checkpoint is incomplete")
        self.reset()
        for key, state in self._states.items():
            text_key = f"{key[0]}:{key[1]}"
            state.restore(states[text_key])
            self._histories[key].extend(float(value) for value in histories[text_key])
        for text_key, value in dict(payload.get("previous_spread", {})).items():
            source, fast, slow = text_key.split(":")
            self._previous_spread[(source, int(fast), int(slow))] = float(value)
        self._atr.restore(payload["atr"])
        self._previous_close = float(payload.get("previous_close", np.nan))
        self._return_window.extend(float(value) for value in payload.get("return_window", ()))


class SMMACompiledAlpha:
    """Stateful SMMA core followed by the existing finite-window GP DSL."""

    def __init__(self, expression: str, manifest: AlphaManifest) -> None:
        self._expression = expression
        self._manifest = manifest
        self._gp = GPCompiledAlpha(expression, manifest)
        self._features = _StreamingFamilyFeatures(self._gp._compiled.variables)
        self._signal = 0.0

    @property
    def manifest(self) -> AlphaManifest:
        return self._manifest

    def update(self, *args: Any, **kwargs: Any) -> float:
        del args
        if bool(kwargs.get("reset", False)):
            self._features.reset_segment()
            self._gp.reset()
        try:
            open_ = float(kwargs["open"])
            high = float(kwargs["high"])
            low = float(kwargs["low"])
            close = float(kwargs["close"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("SMMACompiledAlpha.update requires finite open/high/low/close") from exc
        if not all(np.isfinite(value) for value in (open_, high, low, close)):
            raise ValueError("SMMACompiledAlpha.update requires finite open/high/low/close")
        derived = self._features.update(open_=open_, high=high, low=low, close=close)
        self._signal = self._gp.update(**derived)
        return self._signal

    def reset(self) -> None:
        self._features.reset()
        self._gp.reset()
        self._signal = 0.0

    def get_signal(self) -> float:
        return self._signal

    def snapshot(self) -> dict[str, Any]:
        return {
            "expression": self._expression,
            "features": self._features.snapshot(),
            "gp_buffers": {name: list(buffer) for name, buffer in self._gp._buffers.items()},
            "signal": self._signal,
        }

    def restore(self, payload: Mapping[str, Any]) -> None:
        if payload.get("expression") != self._expression:
            raise ValueError("SMMA alpha checkpoint expression mismatch")
        self.reset()
        self._features.restore(payload["features"])
        gp_buffers = payload.get("gp_buffers")
        if not isinstance(gp_buffers, Mapping) or set(gp_buffers) != set(self._gp._buffers):
            raise ValueError("SMMA alpha checkpoint GP buffers mismatch")
        for name, values in gp_buffers.items():
            self._gp._buffers[str(name)].extend(float(value) for value in values)
        self._signal = float(payload.get("signal", 0.0))
        self._gp._signal = self._signal
