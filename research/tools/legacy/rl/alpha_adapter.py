from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier


PredictFn = Callable[[np.ndarray], float]


@dataclass(frozen=True)
class RLAlphaConfig:
    alpha_id: str
    feature_fields: tuple[str, ...]
    model_path: str | None = None
    hypothesis: str = "RL policy emits directional action signal"
    formula: str = "signal_t = tanh(pi_theta(state_t))"
    paper_refs: tuple[str, ...] = ()
    complexity: str = "O(1)"
    rust_module: str | None = None


class RLAlphaAdapter:
    def __init__(
        self,
        config: RLAlphaConfig,
        *,
        predictor: PredictFn | None = None,
        clip_signal: float = 1.0,
    ) -> None:
        self._config = config
        self._clip = max(0.1, float(clip_signal))
        self._signal = 0.0
        self._predictor = predictor or _build_predictor(config.model_path)

    @property
    def manifest(self) -> AlphaManifest:
        return AlphaManifest(
            alpha_id=self._config.alpha_id,
            hypothesis=self._config.hypothesis,
            formula=self._config.formula,
            paper_refs=self._config.paper_refs,
            data_fields=self._config.feature_fields,
            complexity=self._config.complexity,
            status=AlphaStatus.DRAFT,
            tier=AlphaTier.RL,
            rust_module=self._config.rust_module,
        )

    def update(self, **tick_data: Any) -> float:
        fields = self._config.feature_fields
        feats = np.zeros(len(fields), dtype=np.float64)
        for i, field in enumerate(fields):
            raw = tick_data.get(field, 0.0)
            try:
                feats[i] = float(raw)
            except (TypeError, ValueError):
                feats[i] = 0.0
        score = float(self._predictor(feats))
        clipped = float(np.clip(score, -self._clip, self._clip))
        self._signal = clipped / self._clip
        return self._signal

    def reset(self) -> None:
        self._signal = 0.0

    def get_signal(self) -> float:
        return float(self._signal)


def _build_predictor(model_path: str | None) -> PredictFn:
    if not model_path:
        return lambda x: float(np.mean(x)) if x.size else 0.0

    path = Path(model_path)
    if not path.exists():
        raise FileNotFoundError(f"RL model not found: {model_path}")

    if path.suffix == ".npz":
        bundle = np.load(path, allow_pickle=False)
        if "weights" not in bundle:
            raise ValueError(f"NPZ RL model missing 'weights': {model_path}")
        weights = np.asarray(bundle["weights"], dtype=np.float64).reshape(-1)
        bias = float(bundle["bias"]) if "bias" in bundle else 0.0

        def linear_predict(x: np.ndarray) -> float:
            n = min(weights.size, x.size)
            if n == 0:
                return bias
            return float(np.dot(weights[:n], x[:n]) + bias)

        return linear_predict

    if path.suffix == ".onnx":
        try:
            import onnxruntime as ort
        except Exception as exc:
            raise RuntimeError("onnxruntime is required to load ONNX RL models") from exc
        session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
        input_name = session.get_inputs()[0].name
        output_name = session.get_outputs()[0].name

        def onnx_predict(x: np.ndarray) -> float:
            row = np.asarray(x, dtype=np.float32).reshape(1, -1)
            out = session.run([output_name], {input_name: row})[0]
            return float(np.asarray(out, dtype=np.float64).reshape(-1)[0])

        return onnx_predict

    raise ValueError(f"Unsupported RL model format: {model_path}")
