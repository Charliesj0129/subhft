from __future__ import annotations

import warnings as _warnings_mod
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# SOP governance — canonical role and skill name registries.
# These are the only accepted values for AlphaManifest.roles_used and
# skills_used.  Unknown values trigger a warnings.warn at manifest
# construction time (warn-only, non-blocking).
# ---------------------------------------------------------------------------

VALID_ROLES: frozenset[str] = frozenset({
    "planner",
    "architect",
    "code-reviewer",
    "refactor-cleaner",
})

VALID_SKILLS: frozenset[str] = frozenset({
    "iterative-retrieval",
    "validation-gate",
    "hft-backtester",
    "paper_trader",
    "rust_feature_engineering",
})


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
    # Latency realism governance (CLAUDE.md constitution requirement).
    # Must reference a named latency profile (e.g. "shioaji_sim_p95") before Gate D.
    # Profiles are defined in docs/architecture/latency-baseline-shioaji-sim-vs-system.md.
    # None = NOT promotion-ready (blocks Gate D with a warning).
    latency_profile: str | None = None
    # Research process attribution (SOP governance).
    # roles_used: which SOP roles were applied (planner, architect, code-reviewer, refactor-cleaner).
    # skills_used: which SOP skills were applied (iterative-retrieval, validation-gate,
    #              hft-backtester, paper_trader, rust_feature_engineering).
    # Empty tuples are valid (warn-only at Gate A); not enforced for DRAFT status.
    roles_used: tuple[str, ...] = field(default_factory=tuple)
    skills_used: tuple[str, ...] = field(default_factory=tuple)
    # Feature set version governance (Stage 8 live parity).
    # Must match src/hft_platform/feature/registry.FEATURE_SET_VERSION before Gate D.
    # None = no feature set dependency declared (acceptable for signal-only alphas).
    feature_set_version: str | None = None

    def __post_init__(self) -> None:
        bad_roles = set(self.roles_used) - VALID_ROLES
        bad_skills = set(self.skills_used) - VALID_SKILLS
        if bad_roles or bad_skills:
            _warnings_mod.warn(
                f"Unknown roles={bad_roles or set()} skills={bad_skills or set()} "
                f"in alpha_id={self.alpha_id!r}. "
                f"Valid roles={sorted(VALID_ROLES)}, valid skills={sorted(VALID_SKILLS)}",
                stacklevel=2,
            )

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
            latency_profile=str(data["latency_profile"]) if data.get("latency_profile") else None,
            roles_used=tuple(str(r) for r in data.get("roles_used", ())),
            skills_used=tuple(str(s) for s in data.get("skills_used", ())),
            feature_set_version=str(data["feature_set_version"]) if data.get("feature_set_version") else None,
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
    latency_profile: dict[str, Any] | None = None
    walk_forward_sharpe_mean: float | None = None
    walk_forward_sharpe_std: float | None = None
    walk_forward_sharpe_min: float | None = None
    walk_forward_consistency_pct: float | None = None
    stat_bh_n_survived: int | None = None
    stat_bh_method: str | None = None
    stat_bds_pvalue: float | None = None
    # Stage 6 cost sensitivity: signal_magnitude / min_profitable_spread.
    # Computed from backtest result when avg_spread_cost is available; else None.
    cost_sensitivity_ratio: float | None = None
    data_fingerprint: str | None = None
    rng_seed: int | None = None
    generator_script: str | None = None
    data_ul: int | None = None
    regime_ic: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scorecard":
        raw_latency = data.get("latency_profile")
        latency_profile = (
            {str(k): v for k, v in dict(raw_latency).items()}
            if isinstance(raw_latency, dict)
            else None
        )
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
            latency_profile=latency_profile,
            walk_forward_sharpe_mean=_to_optional_float(data.get("walk_forward_sharpe_mean")),
            walk_forward_sharpe_std=_to_optional_float(data.get("walk_forward_sharpe_std")),
            walk_forward_sharpe_min=_to_optional_float(data.get("walk_forward_sharpe_min")),
            walk_forward_consistency_pct=_to_optional_float(data.get("walk_forward_consistency_pct")),
            stat_bh_n_survived=_to_optional_int(data.get("stat_bh_n_survived")),
            stat_bh_method=str(data["stat_bh_method"]) if data.get("stat_bh_method") else None,
            stat_bds_pvalue=_to_optional_float(data.get("stat_bds_pvalue")),
            cost_sensitivity_ratio=_to_optional_float(data.get("cost_sensitivity_ratio")),
            data_fingerprint=str(data["data_fingerprint"]) if data.get("data_fingerprint") else None,
            rng_seed=_to_optional_int(data.get("rng_seed")),
            generator_script=str(data["generator_script"]) if data.get("generator_script") else None,
            data_ul=_to_optional_int(data.get("data_ul")),
            regime_ic={k: float(v) for k, v in dict(data.get("regime_ic", {})).items()},
        )


def _to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
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
