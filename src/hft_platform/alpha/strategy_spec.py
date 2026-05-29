"""Strategy-spec loader and validator (goal 完成狀態 §3).

Every alpha candidate must declare a fixed-shape ``spec.yaml`` before
any backtest runs.  This module owns the schema check: it does NOT
mutate the spec or supply defaults — missing/empty fields are errors,
because goal 限制 §4 forbids fabricating cost / sample assumptions.

API:
    REQUIRED_TOP_LEVEL_FIELDS : tuple[str, ...]
    ALLOWED_TIMEFRAMES        : frozenset[str]
    ALLOWED_FREQUENCY_CLASSES : frozenset[str]

    load_spec(path) -> dict
    validate_spec(spec) -> list[str]   # empty == valid

Multi-leg note (goal §2): ``instrument`` may be a single string OR a
list of strings; lists of length 1 are still considered single-leg and
flagged so the operator picks one shape.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REQUIRED_TOP_LEVEL_FIELDS: tuple[str, ...] = (
    "strategy_name",
    "market",
    "instrument",
    "hypothesis",
    "timeframe",
    "holding_period",
    "frequency_class",
    "entry_rule",
    "exit_rule",
    "position_sizing",
    "risk_control",
    "cost_model",
    "validation_plan",
)

ALLOWED_TIMEFRAMES: frozenset[str] = frozenset({"tick", "1s", "5s", "1m", "5m", "15m", "60m", "1d"})

ALLOWED_FREQUENCY_CLASSES: frozenset[str] = frozenset({"minute", "intraday_hft", "overnight"})

ALLOWED_MARKETS: frozenset[str] = frozenset({"TAIFEX"})

_RISK_REQUIRED: tuple[str, ...] = (
    "max_position",
    "max_drawdown_pts",
    "force_flat_rule",
)
_COST_REQUIRED: tuple[str, ...] = (
    "fee_bps",
    "tax_bps",
    "slippage_pts",
    "latency_profile",
)
_VALIDATION_REQUIRED: tuple[str, ...] = (
    "data_range",
    "oos_split",
    "sample_targets",
    "required_gates",
    "net_edge_floor_pts",
)
_SAMPLE_TARGETS_REQUIRED: tuple[str, ...] = (
    "min_round_trips",
    "min_oos_trading_days",
)


def load_spec(path: str | Path) -> dict[str, Any]:
    """Parse a YAML spec file.  Raises FileNotFoundError / yaml.YAMLError."""
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"spec at {p} did not parse to a mapping")
    return data


def _is_empty(value: Any) -> bool:
    """Empty == None or empty str/list/dict.  0 and False are NOT empty."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False


def _check_top_level_presence(spec: dict, errors: list[str]) -> None:
    for field in REQUIRED_TOP_LEVEL_FIELDS:
        if field not in spec or _is_empty(spec.get(field)):
            errors.append(f"missing or empty required field: {field!r}")


def _check_market(spec: dict, errors: list[str]) -> None:
    market = spec.get("market")
    if isinstance(market, str) and market not in ALLOWED_MARKETS:
        errors.append(f"market={market!r} not in allowed set {sorted(ALLOWED_MARKETS)}")


def _check_timeframe(spec: dict, errors: list[str]) -> None:
    tf = spec.get("timeframe")
    if isinstance(tf, str) and tf not in ALLOWED_TIMEFRAMES:
        errors.append(f"timeframe={tf!r} not in allowed set {sorted(ALLOWED_TIMEFRAMES)}")


def _check_frequency_class(spec: dict, errors: list[str]) -> None:
    fc = spec.get("frequency_class")
    if isinstance(fc, str) and fc not in ALLOWED_FREQUENCY_CLASSES:
        errors.append(f"frequency_class={fc!r} not in allowed set {sorted(ALLOWED_FREQUENCY_CLASSES)}")


def _check_instrument(spec: dict, errors: list[str]) -> None:
    inst = spec.get("instrument")
    if isinstance(inst, str):
        return
    if isinstance(inst, list):
        if len(inst) < 2:
            errors.append("instrument list of length<2 — use a single string for single-leg candidates")
        for i, leg in enumerate(inst):
            if not isinstance(leg, str) or not leg.strip():
                errors.append(f"instrument[{i}] is not a non-empty string")
        return
    if inst is not None:
        errors.append("instrument must be a string (single-leg) or list of strings (multi-leg)")


def _check_subblock(spec: dict, key: str, required: tuple[str, ...], errors: list[str]) -> None:
    block = spec.get(key)
    if not isinstance(block, dict):
        return
    for field in required:
        if field not in block or _is_empty(block.get(field)):
            errors.append(f"{key}.{field}: missing or empty")


def _check_validation_block(spec: dict, errors: list[str]) -> None:
    block = spec.get("validation_plan")
    if not isinstance(block, dict):
        return
    _check_subblock(spec, "validation_plan", _VALIDATION_REQUIRED, errors)
    targets = block.get("sample_targets")
    if isinstance(targets, dict):
        for field in _SAMPLE_TARGETS_REQUIRED:
            if field not in targets or _is_empty(targets.get(field)):
                errors.append(f"validation_plan.sample_targets.{field}: missing or empty")
    gates = block.get("required_gates")
    if isinstance(gates, list):
        if len(gates) == 0:
            errors.append("validation_plan.required_gates: must list >=1 gate")
        for i, g in enumerate(gates):
            if not isinstance(g, str) or not g.strip():
                errors.append(f"validation_plan.required_gates[{i}] not a string")
    floor = block.get("net_edge_floor_pts")
    if isinstance(floor, (int, float)) and floor < 10.0:
        errors.append(
            "validation_plan.net_edge_floor_pts < 10.0 — goal 限制 §3 forbids relaxing the > 10 pts/trade bar"
        )


def validate_spec(spec: dict[str, Any]) -> list[str]:
    """Return a list of human-readable error strings; empty list == valid.

    Defensive on every branch — never raises on shape errors so a
    candidate authoring tool can surface every gap in one pass.
    """
    errors: list[str] = []
    if not isinstance(spec, dict):
        return ["spec must be a mapping"]

    _check_top_level_presence(spec, errors)
    _check_market(spec, errors)
    _check_timeframe(spec, errors)
    _check_frequency_class(spec, errors)
    _check_instrument(spec, errors)
    _check_subblock(spec, "risk_control", _RISK_REQUIRED, errors)
    _check_subblock(spec, "cost_model", _COST_REQUIRED, errors)
    _check_validation_block(spec, errors)
    return errors


def is_multi_leg(spec: dict[str, Any]) -> bool:
    """True iff this spec declares a multi-leg instrument."""
    inst = spec.get("instrument")
    return isinstance(inst, list) and len(inst) >= 2


def load_spec_provenance(
    alpha_id_or_path: str | Path,
    root: str | Path = "research/alphas",
) -> dict[str, Any] | None:
    """One-call helper: locate a candidate spec.yaml and project it onto
    the audit-row provenance triple.

    Resolution order:
      1. If ``alpha_id_or_path`` is a path to an existing file, load it.
      2. Else treat it as an alpha_id and look for ``<root>/<id>/spec.yaml``.
      3. Else return ``None`` so the caller can omit ``spec_provenance``
         from the result_payload (writers treat ``None`` as opt-out).

    Load failures (parse error, non-mapping) return ``None`` rather than
    raising — pipeline callers running across many candidates can keep
    going.  The strict validation gate is ``hft_platform.alpha.spec_check``;
    this helper is the metadata read-side and stays permissive.
    """
    p = Path(alpha_id_or_path)
    spec_path: Path
    if p.is_file():
        spec_path = p
    else:
        candidate = Path(root) / str(alpha_id_or_path) / "spec.yaml"
        if not candidate.is_file():
            return None
        spec_path = candidate
    try:
        spec = load_spec(spec_path)
    except (ValueError, OSError):
        return None
    except Exception:  # noqa: BLE001 — defensive: yaml.YAMLError etc.
        return None
    return extract_provenance(spec)


def extract_provenance(spec: dict[str, Any]) -> dict[str, Any]:
    """Project a candidate spec onto the audit-row provenance triple.

    Returns the dict shape that ``sub_gate_audit.build_record`` expects
    in ``spec_provenance`` (Round 17): ``data_range``,
    ``cost_model_id``, ``required_gates``.

    ``cost_model_id`` is synthesised from cost_model so the audit log
    records a single human-comparable id rather than a free-form dict.
    The format ``"<latency_profile>+<fee_bps>bp/<tax_bps>bp/<slip>pts"``
    keeps the four cost knobs visible at a glance — any drift on any
    knob produces a different id so ``audit_cli.compare`` can flag it.

    Defensive on every branch: missing keys collapse to "" / [] rather
    than raise, because this helper runs on partially-filled spec
    drafts during scaffolding.
    """
    if not isinstance(spec, dict):
        return {"data_range": "", "cost_model_id": "", "required_gates": []}
    vp = spec.get("validation_plan") if isinstance(spec.get("validation_plan"), dict) else {}
    cost = spec.get("cost_model") if isinstance(spec.get("cost_model"), dict) else {}
    data_range = str(vp.get("data_range") or "")
    raw_gates = vp.get("required_gates") if isinstance(vp.get("required_gates"), list) else []
    required_gates = [str(g) for g in raw_gates if isinstance(g, (str, int, float))]
    if cost:
        latency = str(cost.get("latency_profile") or "unspecified")
        fee = cost.get("fee_bps")
        tax = cost.get("tax_bps")
        slip = cost.get("slippage_pts")
        cost_model_id = f"{latency}+{fee}bp/{tax}bp/{slip}pts"
    else:
        cost_model_id = ""
    return {
        "data_range": data_range,
        "cost_model_id": cost_model_id,
        "required_gates": required_gates,
    }
