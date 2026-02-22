from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from research.registry.alpha_registry import AlphaRegistry
from research.registry.schemas import AlphaProtocol


@dataclass
class RegistryFeatureProvider:
    alpha_ids: tuple[str, ...]
    _alphas: dict[str, AlphaProtocol]

    @property
    def feature_names(self) -> tuple[str, ...]:
        return self.alpha_ids

    @classmethod
    def from_registry(
        cls,
        *,
        alphas_dir: str = "research/alphas",
        include_ids: list[str] | None = None,
    ) -> "RegistryFeatureProvider":
        registry = AlphaRegistry()
        loaded = registry.discover(alphas_dir)
        if include_ids:
            selected_ids = [alpha_id for alpha_id in include_ids if alpha_id in loaded]
        else:
            selected_ids = sorted(loaded)
        selected = {alpha_id: loaded[alpha_id] for alpha_id in selected_ids}
        return cls(alpha_ids=tuple(selected_ids), _alphas=selected)

    def reset(self) -> None:
        for alpha in self._alphas.values():
            alpha.reset()

    def update(self, **tick_data: Any) -> np.ndarray:
        if not self.alpha_ids:
            return np.zeros(0, dtype=np.float32)
        out = np.zeros(len(self.alpha_ids), dtype=np.float32)
        for i, alpha_id in enumerate(self.alpha_ids):
            alpha = self._alphas[alpha_id]
            out[i] = float(alpha.update(**tick_data))
        return out

    def update_from_mapping(self, payload: Mapping[str, Any]) -> np.ndarray:
        return self.update(**dict(payload))
