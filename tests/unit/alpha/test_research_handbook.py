"""The research handbook's gate tables match the strict v2 profile and the sub-gate registry.

`docs/runbooks/alpha-development-workflow.md` copies three tables from
`config/research/profiles/vm_ul6_strict_v2.yaml` and
`src/hft_platform/alpha/_sub_gates/`; these tests fail when either side changes
without the other (research v2 C6).
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from pathlib import Path

import pytest
import yaml

import hft_platform.alpha._sub_gates as sub_gates
from hft_platform.alpha.outcomes import Outcome, Verdict

_ROOT = Path(__file__).resolve().parents[3]
_HANDBOOK = _ROOT / "docs" / "runbooks" / "alpha-development-workflow.md"
_PROFILE = _ROOT / "config" / "research" / "profiles" / "vm_ul6_strict_v2.yaml"


def _rows(table: str) -> list[list[str]]:
    text = _HANDBOOK.read_text(encoding="utf-8")
    match = re.search(rf"<!-- handbook:{table}:begin -->\n(.*?)\n<!-- handbook:{table}:end -->", text, re.S)
    assert match, f"handbook table {table!r} not found"
    lines = match.group(1).splitlines()[2:]  # header and separator
    return [[cell.strip().strip("`") for cell in line.strip("|").split("|")] for line in lines]


def _cell(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else "[]"
    return str(value)


def _registered_sub_gates() -> dict[str, tuple[str, frozenset[str]]]:
    found: dict[str, tuple[str, frozenset[str]]] = {}
    for info in pkgutil.iter_modules(sub_gates.__path__):
        module = importlib.import_module(f"{sub_gates.__name__}.{info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            name = getattr(cls, "name", None)
            applies_to = getattr(cls, "applies_to", None)
            if cls.__module__ == module.__name__ and isinstance(name, str) and applies_to:
                found[name] = (f"{info.name}.py", frozenset(applies_to))
    return found


@pytest.fixture(scope="module")
def profile() -> dict:
    return yaml.safe_load(_PROFILE.read_text(encoding="utf-8"))


def test_handbook_sub_gate_table_lists_every_registered_sub_gate_with_its_strict_role(profile: dict) -> None:
    registered = _registered_sub_gates()
    blocking = set(profile["blocking_sub_gates"])
    deferrable = profile.get("deferrable") or {}

    rows = {row[0]: row[1:] for row in _rows("subgates")}

    assert set(rows) == set(registered)
    for name, (module, audience, role) in rows.items():
        assert module == registered[name][0]
        assert frozenset(audience.split(" + ")) == registered[name][1]
        expected = "blocking" if name in blocking else "advisory"
        if name in deferrable:
            expected = f"blocking, deferrable to {deferrable[name]}"
        assert role.replace("`", "") == expected, name


def test_handbook_threshold_table_equals_the_profile_thresholds(profile: dict) -> None:
    taker, maker = profile["thresholds"]["taker"], profile["thresholds"]["maker"]

    rows = {row[0]: (row[1], row[2]) for row in _rows("thresholds")}

    assert set(rows) == set(taker) | set(maker)
    for key, (taker_cell, maker_cell) in rows.items():
        assert taker_cell == _cell(taker.get(key)), key
        assert maker_cell == _cell(maker.get(key)), key


def test_handbook_pipeline_table_equals_the_profile_overrides_and_defaults(profile: dict) -> None:
    overrides, defaults = profile["pipeline_overrides"], profile["pipeline_baseline_defaults"]

    rows = {row[0]: (row[1], row[2]) for row in _rows("pipeline")}

    assert set(rows) == set(overrides)
    for key, (strict_cell, default_cell) in rows.items():
        assert strict_cell == _cell(overrides[key]), key
        assert default_cell == _cell(defaults.get(key)), key


def test_handbook_vocabulary_names_every_outcome_and_verdict() -> None:
    text = _HANDBOOK.read_text(encoding="utf-8")

    for word in [*(o.value for o in Outcome), *(v.value for v in Verdict)]:
        assert f"| `{word}` |" in text, word


def test_handbook_has_no_rust_live_stage() -> None:
    text = _HANDBOOK.read_text(encoding="utf-8")

    assert "Live (Rust)" not in text
    assert "rust_core/src/" not in text
