"""Hard gates, final_score, and promotion (``scoring_version = score_v1``, spec §13/§14).

Gate semantics
--------------
Gates run on train+validation metrics only (test never feeds back).  Single-
split gate inputs use the TRAIN split (most days, and the yaml names the
no_signal floor ``train_ic_tstat_abs_min``); the explicit cross-split check is
the train/validation direction contradiction inside ``sign_unstable``.
First failing gate assigns the primary ``death_reason``; the full trace is
kept in ``gates_failed``.

The approved maker gate (``cost_proxy_maker``) is evaluated AFTER the frozen
taker gate and maps to the same ``COST_KILLED``.  Because the maker survival
score is ≥ the taker score by construction at an equal threshold, it can never
fail alone — its value is the trace: a candidate failing taker but passing
maker is "maker-rescuable"; failing both is dead under any execution.

``REGIME_ONLY`` is a reserved death reason with no score_v1 gate
(``scoring_v1.yaml`` is the gate source of truth); regime ICs are recorded for
diagnostics only.

final_score components (each normalized to [0,1], weighted per yaml):
predictive = min(|validation ic_tstat|, cap)/cap; stability =
validation sign_consistency; cost = min(cost_survival_score, cap)/cap;
latency = clip(latency_1ms_score, 0, 1); fragility = one_day_concentration;
turnover = min(turnover_proxy, cap)/cap; complexity =
min(signal_node_count, cap)/cap.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from research.candidate_loop.schema import DeathReason, Status

# Gate evaluation order is part of score_v1 (first-failure-wins).
GATE_ORDER = (
    "no_signal",
    "sign_unstable",
    "cost_proxy_taker",
    "cost_proxy_maker",
    "latency_1ms",
    "one_day_only",
)

GATE_DEATH_REASONS: dict[str, DeathReason] = {
    "no_signal": DeathReason.NO_SIGNAL,
    "sign_unstable": DeathReason.SIGN_UNSTABLE,
    "cost_proxy_taker": DeathReason.COST_KILLED,
    "cost_proxy_maker": DeathReason.COST_KILLED,
    "latency_1ms": DeathReason.LATENCY_KILLED,
    "one_day_only": DeathReason.ONE_DAY_ONLY,
}


@dataclass(frozen=True)
class ScoringConfig:
    scoring_version: str
    signal_std_zero_day_fraction_max: float
    train_ic_tstat_abs_min: float
    sign_consistency_min: float
    contradiction_ic_floor: float
    cost_survival_min: float
    maker_cost_survival_min: float
    q_hat_table_path: str
    q_hat_symbol: str
    maker_cost_assumption_version: str
    latency_retention_min: float
    one_day_concentration_max: float
    score_weights: dict[str, float]
    ic_tstat_cap: float
    cost_survival_cap: float
    turnover_cap_flips_per_day: float
    complexity_node_cap: int
    promotion_top_fraction: float
    watchlist_next_decile: bool
    watchlist_ic_tstat_min: float


def load_scoring_config(path: Path) -> ScoringConfig:
    raw = yaml.safe_load(path.read_text())
    gates = raw["gates"]
    norm = raw["normalization"]
    promo = raw["promotion"]
    return ScoringConfig(
        scoring_version=str(raw["scoring_version"]),
        signal_std_zero_day_fraction_max=float(gates["no_signal"]["signal_std_zero_day_fraction_max"]),
        train_ic_tstat_abs_min=float(gates["no_signal"]["train_ic_tstat_abs_min"]),
        sign_consistency_min=float(gates["sign_unstable"]["sign_consistency_min"]),
        contradiction_ic_floor=float(gates["sign_unstable"]["contradiction_ic_floor"]),
        cost_survival_min=float(gates["cost_proxy_taker"]["cost_survival_min"]),
        maker_cost_survival_min=float(gates["cost_proxy_maker"]["maker_cost_survival_min"]),
        q_hat_table_path=str(gates["cost_proxy_maker"]["q_hat_table"]),
        q_hat_symbol=str(gates["cost_proxy_maker"]["q_hat_symbol"]),
        maker_cost_assumption_version=str(gates["cost_proxy_maker"]["maker_cost_assumption_version"]),
        latency_retention_min=float(gates["latency_1ms"]["retention_min"]),
        one_day_concentration_max=float(gates["one_day_only"]["one_day_concentration_max"]),
        score_weights={str(k): float(v) for k, v in raw["score_weights"].items()},
        ic_tstat_cap=float(norm["ic_tstat_cap"]),
        cost_survival_cap=float(norm["cost_survival_cap"]),
        turnover_cap_flips_per_day=float(norm["turnover_cap_flips_per_day"]),
        complexity_node_cap=int(norm["complexity_node_cap"]),
        promotion_top_fraction=float(promo["top_fraction"]),
        watchlist_next_decile=bool(promo["watchlist_next_decile"]),
        watchlist_ic_tstat_min=float(promo["watchlist_ic_tstat_min"]),
    )


# ---------------------------------------------------------------------------
# Hard gates.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateOutcome:
    gates_passed: tuple[str, ...]
    gates_failed: tuple[str, ...]
    death_reason: DeathReason | None  # first failure, None when all pass

    @property
    def survived(self) -> bool:
        return not self.gates_failed


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def apply_hard_gates(train: dict[str, Any], validation: dict[str, Any], cfg: ScoringConfig) -> GateOutcome:
    """Evaluate score_v1 hard gates in order over train(+validation) metrics."""
    failures: dict[str, bool] = {}

    failures["no_signal"] = (
        float(train.get("signal_std_zero_day_fraction", 1.0)) > cfg.signal_std_zero_day_fraction_max
        or abs(float(train.get("ic_tstat", 0.0))) < cfg.train_ic_tstat_abs_min
    )

    train_ic = float(train.get("ic", 0.0))
    val_ic = float(validation.get("ic", 0.0))
    contradiction = (
        abs(train_ic) > cfg.contradiction_ic_floor
        and abs(val_ic) > cfg.contradiction_ic_floor
        and _sign(train_ic) != _sign(val_ic)
    )
    failures["sign_unstable"] = float(train.get("sign_consistency", 0.0)) < cfg.sign_consistency_min or contradiction

    failures["cost_proxy_taker"] = float(train.get("cost_survival_score", 0.0)) < cfg.cost_survival_min
    # Maker view: lower-bound cost (<= taker), same threshold — can only fail
    # when taker also failed; recorded for the maker-rescuable trace.
    failures["cost_proxy_maker"] = float(train.get("maker_cost_survival_score", 0.0)) < cfg.maker_cost_survival_min

    # Signed retention: a sign flip is negative, so one comparison covers both.
    failures["latency_1ms"] = float(train.get("latency_1ms_score", 0.0)) < cfg.latency_retention_min

    failures["one_day_only"] = float(train.get("one_day_concentration", 1.0)) > cfg.one_day_concentration_max

    passed = tuple(g for g in GATE_ORDER if not failures[g])
    failed = tuple(g for g in GATE_ORDER if failures[g])
    death = GATE_DEATH_REASONS[failed[0]] if failed else None
    return GateOutcome(gates_passed=passed, gates_failed=failed, death_reason=death)


def is_maker_rescuable(outcome: GateOutcome) -> bool:
    """Dead at taker cost but alive under the maker view (failure_summary stat)."""
    return "cost_proxy_taker" in outcome.gates_failed and "cost_proxy_maker" not in outcome.gates_failed


# ---------------------------------------------------------------------------
# final_score (score_v1).
# ---------------------------------------------------------------------------


def compute_score_components(
    validation: dict[str, Any], signal_node_count: int, cfg: ScoringConfig
) -> dict[str, float]:
    def cap01(value: float, cap: float) -> float:
        return min(max(value, 0.0), cap) / cap if cap > 0 else 0.0

    return {
        "predictive_score": cap01(abs(float(validation.get("ic_tstat", 0.0))), cfg.ic_tstat_cap),
        "stability_score": min(max(float(validation.get("sign_consistency", 0.0)), 0.0), 1.0),
        "cost_survival_score": cap01(float(validation.get("cost_survival_score", 0.0)), cfg.cost_survival_cap),
        "latency_survival_score": min(max(float(validation.get("latency_1ms_score", 0.0)), 0.0), 1.0),
        "fragility_penalty": min(max(float(validation.get("one_day_concentration", 1.0)), 0.0), 1.0),
        "turnover_penalty": cap01(float(validation.get("turnover_proxy", 0.0)), cfg.turnover_cap_flips_per_day),
        "complexity_penalty": cap01(float(signal_node_count), float(cfg.complexity_node_cap)),
    }


def compute_final_score(
    validation: dict[str, Any], signal_node_count: int, cfg: ScoringConfig
) -> tuple[float, dict[str, float]]:
    components = compute_score_components(validation, signal_node_count, cfg)
    final = sum(cfg.score_weights.get(name, 0.0) * value for name, value in components.items())
    return final, components


# ---------------------------------------------------------------------------
# Promotion (§14).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoredCandidate:
    alpha_id: str
    family: str
    gate: GateOutcome
    final_score: float
    validation_ic_tstat: float


def assign_statuses(rows: list[ScoredCandidate], cfg: ScoringConfig) -> dict[str, Status]:
    """REJECTED / WATCHLIST / PROMOTED per spec §14 (deterministic ordering).

    Survivors ranked by validation final_score (ties broken by alpha_id);
    PROMOTED = top ceil(top_fraction × survivors); WATCHLIST = the next decile
    plus below-cut survivors with |validation ic_tstat| ≥ threshold; everything
    else (including all gate failures) REJECTED.
    """
    statuses: dict[str, Status] = {r.alpha_id: Status.REJECTED for r in rows}
    survivors = [r for r in rows if r.gate.survived]
    if not survivors:
        return statuses
    ranked = sorted(survivors, key=lambda r: (-r.final_score, r.alpha_id))
    n_promoted = math.ceil(cfg.promotion_top_fraction * len(ranked))
    for r in ranked[:n_promoted]:
        statuses[r.alpha_id] = Status.PROMOTED
    if cfg.watchlist_next_decile:
        n_decile = math.ceil(0.10 * len(ranked))
        for r in ranked[n_promoted : n_promoted + n_decile]:
            statuses[r.alpha_id] = Status.WATCHLIST
    for r in ranked[n_promoted:]:
        if statuses[r.alpha_id] == Status.REJECTED and abs(r.validation_ic_tstat) >= cfg.watchlist_ic_tstat_min:
            statuses[r.alpha_id] = Status.WATCHLIST
    return statuses


def direction_match(ic_a: float, ic_b: float) -> bool:
    """Both ICs nonzero and same-signed (train/validation, validation/test flags)."""
    return _sign(ic_a) != 0 and _sign(ic_a) == _sign(ic_b)


__all__ = [
    "GATE_DEATH_REASONS",
    "GATE_ORDER",
    "GateOutcome",
    "ScoredCandidate",
    "ScoringConfig",
    "apply_hard_gates",
    "assign_statuses",
    "compute_final_score",
    "compute_score_components",
    "direction_match",
    "is_maker_rescuable",
    "load_scoring_config",
]
