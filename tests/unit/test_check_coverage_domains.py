"""The per-package coverage floors only ratchet up, and the gate script does what it says."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_coverage_domains.py"

# The floors committed with the ratchet. A floor may be raised without touching this table; lowering one means
# editing it as well, which is the "architecture review" the script header asks for.
MINIMUM_FLOORS = {
    "risk": 92.0,
    "order": 90.0,
    "execution": 92.0,
    "gateway": 94.0,
    "recorder": 87.0,
    "alpha": 90.0,
    ".": 88.0,
    "ipc": 81.0,
    "strategies.alpha": 22.0,
}


def _load_module():
    spec = importlib.util.spec_from_file_location("check_coverage_domains", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _coverage_xml(path: Path, rates: dict[str, float]) -> Path:
    packages = "".join(f'<package name="{name}" line-rate="{rate / 100:.6f}"/>' for name, rate in rates.items())
    path.write_text(f'<?xml version="1.0"?><coverage><packages>{packages}</packages></coverage>', encoding="utf-8")
    return path


def test_floors_never_drop_below_the_ratcheted_minimums() -> None:
    mod = _load_module()
    lowered = {
        name: mod.PACKAGE_FLOORS.get(name)
        for name, minimum in MINIMUM_FLOORS.items()
        if mod.PACKAGE_FLOORS.get(name, 0.0) < minimum
    }
    assert lowered == {}


def test_gate_passes_every_package_exactly_at_its_floor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mod = _load_module()
    report = _coverage_xml(tmp_path / "coverage.xml", dict(mod.PACKAGE_FLOORS))

    assert mod.main(report) == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out
    assert "PASSED" in out


def test_gate_ignores_float_noise_in_the_line_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mod = _load_module()
    monkeypatch.setitem(mod.PACKAGE_FLOORS, "risk", 57.0)
    report = tmp_path / "coverage.xml"
    # 0.57 * 100 is 56.99999999999999 in floating point, a package that sits exactly at its floor
    report.write_text(
        '<?xml version="1.0"?><coverage><packages><package name="risk" line-rate="0.57"/></packages></coverage>',
        encoding="utf-8",
    )

    assert mod.main(report) == 0
    assert "FAIL" not in capsys.readouterr().out


@pytest.mark.parametrize("package", list(MINIMUM_FLOORS))
def test_gate_fails_any_package_a_hundredth_of_a_point_below_its_floor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], package: str
) -> None:
    mod = _load_module()
    rates = dict(mod.PACKAGE_FLOORS)
    rates[package] = mod.PACKAGE_FLOORS[package] - 0.01
    report = _coverage_xml(tmp_path / "coverage.xml", rates)

    assert mod.main(report) == 1
    err = capsys.readouterr().err
    assert "FAILED: 1 package(s) below floor" in err
    assert f"  {package}: " in err


def test_gate_fails_a_package_whose_line_rate_is_missing(tmp_path: Path) -> None:
    mod = _load_module()
    report = tmp_path / "coverage.xml"
    report.write_text(
        '<?xml version="1.0"?><coverage><packages><package name="risk"/></packages></coverage>', encoding="utf-8"
    )

    assert mod.main(report) == 1


@pytest.mark.parametrize("content", [None, "<coverage><packages>"])
def test_gate_exits_2_when_the_report_is_missing_or_unparseable(tmp_path: Path, content: str | None) -> None:
    mod = _load_module()
    report = tmp_path / "coverage.xml"
    if content is not None:
        report.write_text(content, encoding="utf-8")

    assert mod.main(report) == 2


def test_gate_applies_the_default_floor_to_an_unlisted_package(tmp_path: Path) -> None:
    mod = _load_module()
    assert "unlisted_package" not in mod.PACKAGE_FLOORS
    assert mod.DEFAULT_FLOOR >= 70.0  # never below the global gate in pyproject.toml / Makefile

    below = _coverage_xml(tmp_path / "below.xml", {"unlisted_package": mod.DEFAULT_FLOOR - 0.1})
    at = _coverage_xml(tmp_path / "at.xml", {"unlisted_package": mod.DEFAULT_FLOOR})

    assert mod.main(below) == 1
    assert mod.main(at) == 0


def test_script_names_only_existing_agent_files() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    named = {match.rstrip(".") for match in re.findall(r"\.agent/[A-Za-z0-9_./-]+", text)}
    missing = sorted(name for name in named if not (ROOT / name).exists())
    assert missing == []
