"""Profile-driven blocking for Gate C sub-gates.

A `ValidationProfile` carries threshold overrides plus a list of sub-gate
names that must pass for Gate C to mark a run as ``passed``. Loose runs
(profile=None) preserve the existing advisory-only behavior bit-for-bit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger("alpha.validation_profile")


class ProfileValidationError(ValueError):
    """Raised when a profile is structurally invalid (e.g. references an unregistered gate)."""


@dataclass(frozen=True)
class ValidationProfile:
    """Promotion-eligibility profile."""

    name: str
    is_strict: bool
    thresholds: dict[str, dict[str, Any]] = field(default_factory=dict)
    blocking_sub_gates: tuple[str, ...] = ()
    # Stage-2 (2026-05-28): argparse-arg overrides for research.pipeline.
    # Optional — profiles that don't drive the pipeline (e.g. unit-test
    # fixtures) leave these empty.
    pipeline_overrides: dict[str, Any] = field(default_factory=dict)
    pipeline_baseline_defaults: dict[str, Any] = field(default_factory=dict)

    def thresholds_for(self, *, strategy_type: str) -> dict[str, Any]:
        """Return thresholds for the given strategy type (maker|taker)."""
        return dict(self.thresholds.get(strategy_type, {}))


def load_profile(path: str | Path) -> ValidationProfile:
    """Load and validate a profile YAML file.

    Validation:
        - Every name in `blocking_sub_gates` must be present in the live
          sub-gate registry.
        - A profile with `is_strict: true` must list at least one blocking
          sub-gate.

    Raises:
        FileNotFoundError: if `path` does not exist.
        ProfileValidationError: on any structural validation failure.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"profile not found: {p}")

    body = yaml.safe_load(p.read_text()) or {}
    if not isinstance(body, dict):
        raise ProfileValidationError(f"profile {p}: top-level YAML must be a mapping, got {type(body).__name__}")

    name = str(body.get("name", p.stem))
    is_strict = bool(body.get("is_strict", False))

    raw_thresholds = body.get("thresholds") or {}
    if not isinstance(raw_thresholds, dict):
        raise ProfileValidationError(
            f"profile {name!r}: thresholds must be a mapping, got {type(raw_thresholds).__name__}"
        )
    thresholds = raw_thresholds

    raw_blocking = body.get("blocking_sub_gates") or ()
    if not isinstance(raw_blocking, (list, tuple)):
        raise ProfileValidationError(
            f"profile {name!r}: blocking_sub_gates must be a list, got {type(raw_blocking).__name__}"
        )
    blocking = tuple(raw_blocking)

    from hft_platform.alpha._sub_gates import (
        ensure_builtin_sub_gates_registered,
        get_registered_sub_gates,
    )

    ensure_builtin_sub_gates_registered()
    known_names = {g.name for g in get_registered_sub_gates()}
    unknown = [n for n in blocking if n not in known_names]
    if unknown:
        raise ProfileValidationError(f"profile {name!r}: blocking_sub_gates references unregistered gate(s): {unknown}")

    if is_strict and not blocking:
        raise ProfileValidationError(f"profile {name!r}: strict profile must list at least one blocking_sub_gate")

    raw_overrides = body.get("pipeline_overrides") or {}
    if not isinstance(raw_overrides, dict):
        raise ProfileValidationError(
            f"profile {name!r}: pipeline_overrides must be a mapping, got {type(raw_overrides).__name__}"
        )
    raw_baselines = body.get("pipeline_baseline_defaults") or {}
    if not isinstance(raw_baselines, dict):
        raise ProfileValidationError(
            f"profile {name!r}: pipeline_baseline_defaults must be a mapping, got {type(raw_baselines).__name__}"
        )

    logger.info(
        "validation_profile_loaded",
        name=name,
        is_strict=is_strict,
        blocking_gate_count=len(blocking),
        pipeline_override_count=len(raw_overrides),
    )
    return ValidationProfile(
        name=name,
        is_strict=is_strict,
        thresholds=thresholds,
        blocking_sub_gates=blocking,
        pipeline_overrides=dict(raw_overrides),
        pipeline_baseline_defaults=dict(raw_baselines),
    )
