from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class AlphaStatus(str, Enum):
    DRAFT = "DRAFT"
    GATE_A = "GATE_A"
    GATE_B = "GATE_B"
    GATE_C = "GATE_C"
    GATE_D = "GATE_D"
    GATE_E = "GATE_E"
    PRODUCTION = "PRODUCTION"
    DEPRECATED = "DEPRECATED"


class AlphaTier(str, Enum):
    TIER_1 = "TIER_1"
    TIER_2 = "TIER_2"
    ENSEMBLE = "ENSEMBLE"
    RL = "RL"


@dataclass(frozen=True)
class AlphaManifest:
    alpha_id: str
    hypothesis: str
    formula: str
    paper_refs: tuple[str, ...]
    data_fields: tuple[str, ...]
    complexity: str
    status: AlphaStatus = AlphaStatus.DRAFT
    tier: AlphaTier | None = None
    rust_module: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["tier"] = self.tier.value if self.tier else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AlphaManifest":
        return cls(
            alpha_id=str(data["alpha_id"]),
            hypothesis=str(data.get("hypothesis", "")),
            formula=str(data.get("formula", "")),
            paper_refs=tuple(data.get("paper_refs", ())),
            data_fields=tuple(data.get("data_fields", ())),
            complexity=str(data.get("complexity", "O(1)")),
            status=AlphaStatus(str(data.get("status", AlphaStatus.DRAFT.value))),
            tier=AlphaTier(str(data["tier"])) if data.get("tier") else None,
            rust_module=str(data["rust_module"]) if data.get("rust_module") else None,
        )


@dataclass(frozen=True)
class Scorecard:
    sharpe_is: float | None = None
    sharpe_oos: float | None = None
    ic_mean: float | None = None
    ic_std: float | None = None
    turnover: float | None = None
    max_drawdown: float | None = None
    correlation_pool_max: float | None = None
    regime_sharpe: dict[str, float] = field(default_factory=dict)
    capacity_estimate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scorecard":
        return cls(
            sharpe_is=_to_optional_float(data.get("sharpe_is")),
            sharpe_oos=_to_optional_float(data.get("sharpe_oos")),
            ic_mean=_to_optional_float(data.get("ic_mean")),
            ic_std=_to_optional_float(data.get("ic_std")),
            turnover=_to_optional_float(data.get("turnover")),
            max_drawdown=_to_optional_float(data.get("max_drawdown")),
            correlation_pool_max=_to_optional_float(data.get("correlation_pool_max")),
            regime_sharpe={k: float(v) for k, v in dict(data.get("regime_sharpe", {})).items()},
            capacity_estimate=_to_optional_float(data.get("capacity_estimate")),
        )


def _to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@runtime_checkable
class AlphaProtocol(Protocol):
    @property
    def manifest(self) -> AlphaManifest:
        ...

    def update(self, *args: Any, **kwargs: Any) -> float:
        ...

    def reset(self) -> None:
        ...

    def get_signal(self) -> float:
        ...


@runtime_checkable
class BatchAlphaProtocol(AlphaProtocol, Protocol):
    """Optional high-throughput API for batch evaluation on ndarray inputs."""

    def update_batch(self, data: Any) -> Any:
        ...
