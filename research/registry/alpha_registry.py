from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any, Iterable, Mapping

from research.registry.correlation_tracker import CorrelationTracker
from research.registry.schemas import AlphaManifest, AlphaProtocol, AlphaStatus, AlphaTier


class AlphaRegistry:
    """File-system discovery and in-memory registry for alpha artifacts."""

    def __init__(self) -> None:
        self._alphas: dict[str, AlphaProtocol] = {}
        self._errors: list[str] = []

    @property
    def errors(self) -> tuple[str, ...]:
        return tuple(self._errors)

    def register(self, alpha: AlphaProtocol) -> None:
        if not isinstance(alpha, AlphaProtocol):
            raise TypeError("Alpha does not conform to AlphaProtocol")
        manifest = alpha.manifest
        if manifest.alpha_id in self._alphas:
            raise ValueError(f"Duplicate alpha_id already registered: {manifest.alpha_id}")
        self._alphas[manifest.alpha_id] = alpha

    def discover(self, alphas_dir: str | Path = "research/alphas") -> dict[str, AlphaProtocol]:
        base = Path(alphas_dir)
        if not base.exists():
            return dict(self._alphas)

        for impl_path in sorted(base.glob("*/impl.py")):
            if impl_path.parent.name.startswith("_"):
                continue
            try:
                module = importlib.import_module(_to_module_name(impl_path))
            except Exception as exc:
                self._errors.append(f"Failed to import {impl_path}: {exc}")
                continue

            loaded = self._load_module_alphas(module)
            if not loaded:
                self._errors.append(f"No AlphaProtocol implementation discovered in {impl_path}")
        return dict(self._alphas)

    def list_alpha_ids(self) -> list[str]:
        return sorted(self._alphas)

    def list_by_status(self, status: AlphaStatus) -> list[AlphaManifest]:
        return [a.manifest for a in self._alphas.values() if a.manifest.status == status]

    def list_by_tier(self, tier: AlphaTier) -> list[AlphaManifest]:
        return [a.manifest for a in self._alphas.values() if a.manifest.tier == tier]

    def get(self, alpha_id: str) -> AlphaProtocol | None:
        return self._alphas.get(alpha_id)

    def manifests(self) -> list[AlphaManifest]:
        return [a.manifest for a in self._alphas.values()]

    def compute_correlation_matrix(self, signals: Mapping[str, Iterable[float]]) -> dict[str, Any]:
        tracker = CorrelationTracker()
        return tracker.compute_matrix(signals)

    def _load_module_alphas(self, module: Any) -> bool:
        loaded = False
        explicit_cls = getattr(module, "ALPHA_CLASS", None)
        if inspect.isclass(explicit_cls):
            instance = self._try_construct(explicit_cls)
            if instance is not None:
                self._safe_register(instance)
                loaded = True
            return loaded

        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__:
                continue
            instance = self._try_construct(obj)
            if instance is None:
                continue
            if isinstance(instance, AlphaProtocol):
                self._safe_register(instance)
                loaded = True
        return loaded

    def _try_construct(self, cls: type[Any]) -> AlphaProtocol | None:
        try:
            sig = inspect.signature(cls)
        except (TypeError, ValueError):
            return None

        required = [
            p
            for p in sig.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            and p.default is inspect._empty
        ]
        if required:
            return None
        try:
            instance = cls()
        except Exception:
            return None
        if isinstance(instance, AlphaProtocol):
            return instance
        return None

    def _safe_register(self, alpha: AlphaProtocol) -> None:
        try:
            self.register(alpha)
        except Exception as exc:
            self._errors.append(f"Failed to register alpha: {exc}")


def _to_module_name(path: Path) -> str:
    rel = path.with_suffix("")
    return ".".join(rel.parts)
