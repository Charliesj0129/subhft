"""Diff the two profile sources for VM-UL6 (one-shot Stage 2.1 diagnostic).

Surfaces every threshold/override key that lives in both
``research/pipeline.py::_VM_UL6_PROFILE_OVERRIDES`` and
``config/research/profiles/vm_ul6_strict.yaml``, then reports which one is
authoritative today (per entrypoint) and any concrete value drift.

Run once before Stage 2.3 lands and once after to confirm the legacy dict is
gone:

    uv run python scripts/ops/diff_profile_sources.py

Exit code 0 on success (drift table printed); nonzero only on IO error.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_PY = REPO_ROOT / "research" / "pipeline.py"
PROFILE_YAML = REPO_ROOT / "config" / "research" / "profiles" / "vm_ul6_strict.yaml"

# Mapping argparse-key -> YAML lookup path (dotted) for keys that are expected
# to coexist in both sources today.  Anything *not* listed here is considered
# pipeline-only (an argparse override with no YAML counterpart) and is reported
# as ``yaml=<missing>``.
KEY_TO_YAML_PATH: dict[str, str] = {
    "min_sharpe_oos_gate_d": "thresholds.taker.sharpe_oos_min",
    "max_abs_drawdown_gate_d": "thresholds.taker.max_drawdown_pct",  # note unit drift below
    "max_correlation_gate_d": "thresholds.taker.max_correlation",
}

# Some keys differ in unit (fraction in dict vs percent in YAML).  This map
# normalizes them for comparison so we report semantic drift, not unit drift.
UNIT_CONVERSIONS: dict[str, callable] = {
    "max_abs_drawdown_gate_d": lambda v: float(v) * 100.0,  # 0.10 -> 10.0
}


def _load_pipeline_overrides() -> dict[str, Any]:
    src = PIPELINE_PY.read_text(encoding="utf-8")
    # Parse the dict literal between ``_VM_UL6_PROFILE_OVERRIDES`` and the
    # next blank line.  Use ast.literal_eval on a sliced string.
    import ast
    import re

    match = re.search(
        r"_VM_UL6_PROFILE_OVERRIDES:\s*dict\[str,\s*Any\]\s*=\s*(\{[\s\S]+?\n\})",
        src,
    )
    if not match:
        # Dict already removed (post-Stage-2.3) — return empty.
        return {}
    return ast.literal_eval(match.group(1))


def _lookup_yaml(body: dict[str, Any], dotted: str) -> Any:
    cur: Any = body
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


_MISSING = object()


def main(argv: list[str]) -> int:
    if not PIPELINE_PY.exists():
        print(f"pipeline.py not found: {PIPELINE_PY}", file=sys.stderr)
        return 2
    if not PROFILE_YAML.exists():
        print(f"profile YAML not found: {PROFILE_YAML}", file=sys.stderr)
        return 2

    dict_overrides = _load_pipeline_overrides()
    yaml_body = yaml.safe_load(PROFILE_YAML.read_text(encoding="utf-8")) or {}
    yaml_pipeline = (yaml_body.get("pipeline_overrides") or {}) if isinstance(yaml_body, dict) else {}

    rows: list[dict[str, Any]] = []
    keys = sorted(set(dict_overrides) | set(yaml_pipeline) | set(KEY_TO_YAML_PATH))
    for key in keys:
        dict_value = dict_overrides.get(key, _MISSING)
        yaml_value = yaml_pipeline.get(key, _MISSING)
        threshold_path = KEY_TO_YAML_PATH.get(key)
        threshold_value = _lookup_yaml(yaml_body, threshold_path) if threshold_path else _MISSING

        normalizer = UNIT_CONVERSIONS.get(key)
        compare_dict = normalizer(dict_value) if normalizer and dict_value is not _MISSING else dict_value

        status = "ok"
        if dict_value is _MISSING and yaml_value is _MISSING and threshold_value is _MISSING:
            status = "absent_both"
        elif dict_value is _MISSING:
            status = "yaml_only"
        elif yaml_value is _MISSING and threshold_value is _MISSING:
            status = "dict_only"
        elif yaml_value is not _MISSING and dict_value != yaml_value:
            status = "drift_pipeline_overrides"
        elif (
            threshold_value is not _MISSING
            and compare_dict is not _MISSING
            and compare_dict != threshold_value
        ):
            status = "drift_thresholds"

        rows.append(
            {
                "key": key,
                "dict": _render(dict_value),
                "yaml_pipeline_overrides": _render(yaml_value),
                "yaml_threshold_path": threshold_path or "",
                "yaml_threshold_value": _render(threshold_value),
                "status": status,
            }
        )

    print(json.dumps(rows, indent=2))
    print()
    print("Status summary:")
    summary: dict[str, int] = {}
    for r in rows:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    for k, v in sorted(summary.items()):
        print(f"  {k}: {v}")
    return 0


def _render(value: Any) -> Any:
    if value is _MISSING:
        return "<missing>"
    return value


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
