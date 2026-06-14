"""failure_summary.json → typed per-family steering signals + focus label.

Pure module: the only IO is reading the governor YAML config and the summary
dict the caller already loaded. No ClickHouse, no frozen-loop imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

FOCUS_LABELS: tuple[str, ...] = ("amplify", "maintain", "deprioritize", "retire")


@dataclass(frozen=True)
class GovernorConfig:
    governor_version: str
    model_name: str
    base_url: str
    max_tokens: int
    timeout_seconds: float
    max_retries: int
    temperature: float
    amplify_survival_min: float
    amplify_ic_p50_min: float
    deprioritize_survival_max: float
    near_miss_margin_abs_max: float
    n_target: dict[str, int]


def load_governor_config(path: Path) -> GovernorConfig:
    raw = yaml.safe_load(path.read_text())
    try:
        model = raw["model"]
        focus = raw["focus"]
        cfg = GovernorConfig(
            governor_version=str(raw["governor_version"]),
            model_name=str(model["name"]),
            base_url=str(model["base_url"]),
            max_tokens=int(model["max_tokens"]),
            timeout_seconds=float(model["timeout_seconds"]),
            max_retries=int(model["max_retries"]),
            temperature=float(model["temperature"]),
            amplify_survival_min=float(focus["amplify_survival_min"]),
            amplify_ic_p50_min=float(focus["amplify_ic_p50_min"]),
            deprioritize_survival_max=float(focus["deprioritize_survival_max"]),
            near_miss_margin_abs_max=float(focus["near_miss_margin_abs_max"]),
            n_target={str(k): int(v) for k, v in focus["n_target"].items()},
        )
    except KeyError as exc:
        raise KeyError(f"{path}: missing governor config key {exc}") from exc
    missing = [f for f in FOCUS_LABELS if f not in cfg.n_target]
    if missing:
        raise KeyError(f"{path}: focus.n_target missing labels {missing}")
    return cfg


@dataclass(frozen=True)
class SteeringSignals:
    family: str
    candidates: int
    survival_rate: float
    ic_p10: float
    ic_p50: float
    ic_p90: float
    cost_failure_rate: float
    maker_cost_failure_rate: float
    maker_rescuable_count: int
    duplicate_rate: float
    reduced_day_coverage_count: int
    near_misses: list[dict[str, Any]]
    common_failure_patterns: list[str]


def extract_signals(summary: dict[str, Any], family: str) -> SteeringSignals:
    fam = summary.get("per_family", {}).get(family, {}) or {}
    ic = fam.get("ic_distribution_survivors", {}) or {}
    return SteeringSignals(
        family=family,
        candidates=int(fam.get("candidates", 0)),
        survival_rate=float(fam.get("survival_rate", 0.0)),
        ic_p10=float(ic.get("p10", 0.0)),
        ic_p50=float(ic.get("p50", 0.0)),
        ic_p90=float(ic.get("p90", 0.0)),
        cost_failure_rate=float(fam.get("cost_failure_rate", 0.0)),
        maker_cost_failure_rate=float(fam.get("maker_cost_failure_rate", 0.0)),
        maker_rescuable_count=int(fam.get("maker_rescuable_count", 0)),
        duplicate_rate=float(fam.get("duplicate_rate", 0.0)),
        reduced_day_coverage_count=int(fam.get("reduced_day_coverage_count", 0)),
        near_misses=list(fam.get("near_misses", []) or []),
        common_failure_patterns=list(fam.get("common_failure_patterns", []) or []),
    )


def _has_cheap_near_miss(signals: SteeringSignals, cfg: GovernorConfig) -> bool:
    # near_miss margins are signed distance-to-passing; a single-gate failure has
    # margin < 0. "cheap to flip" = within the band just below the threshold.
    return any(
        float(nm.get("margin", -1.0)) >= -cfg.near_miss_margin_abs_max
        for nm in signals.near_misses
    )


def classify_focus(signals: SteeringSignals, cfg: GovernorConfig) -> str:
    """Deterministic priority: amplify > retire > deprioritize > maintain."""
    rescue = signals.maker_rescuable_count > 0 or _has_cheap_near_miss(signals, cfg)
    if (
        signals.survival_rate >= cfg.amplify_survival_min
        or signals.ic_p50 >= cfg.amplify_ic_p50_min
        or rescue
    ):
        return "amplify"
    # retire = literally zero survivors; a non-zero-but-tiny survival_rate falls through to deprioritize
    if signals.survival_rate == 0.0 and not rescue:
        return "retire"
    if signals.survival_rate <= cfg.deprioritize_survival_max:
        return "deprioritize"
    return "maintain"


def n_target_for(focus: str, cfg: GovernorConfig) -> int:
    return cfg.n_target[focus]


__all__ = [
    "FOCUS_LABELS",
    "GovernorConfig",
    "SteeringSignals",
    "classify_focus",
    "extract_signals",
    "load_governor_config",
    "n_target_for",
]
