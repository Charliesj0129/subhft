"""Every Prometheus rule file in the repo is loaded by the Prometheus container, and every recorded series an
alert reads is defined by a loaded rule.

Prometheus loads only what ``rule_files:`` in ``config/monitoring/prometheus.yml`` names, and only what the compose
``prometheus`` service mounts (``config/monitoring/alerts`` as a directory; ``docker-compose.yml`` is compared with the
locked production compose by ``tests/integration/test_prod_compose_parity.py``, so a rule file belongs in that
directory rather than behind a new volume). A rule file that is neither listed nor mounted is evaluated by nobody,
and a path that matches no file is not an error in Prometheus. An alert
that reads a series such as ``slo:order_success:ratio_rate5m`` that no loaded rule records has no input: it reads as
permanently healthy rather than as broken.

Scope: file wiring and recorded-series names. PromQL semantics need ``promtool test rules``
(``config/monitoring/alerts/rules_test.yaml``), which is not part of ``make test``.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MONITORING = ROOT / "config" / "monitoring"
PROMETHEUS_YML = MONITORING / "prometheus.yml"
COMPOSE_YML = ROOT / "docker-compose.yml"

# A quoted PromQL string (label values such as instance="host:9090" contain colons that are not series names).
_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|`[^`]*`')
# By convention a PromQL identifier that contains a colon is a recording-rule output.
_RECORDED_SERIES = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?::[A-Za-z0-9_]+)+")


def _load(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _prometheus_mounts() -> list[tuple[Path, str]]:
    """(host directory or file, container path) for every volume of the compose ``prometheus`` service."""
    service = _load(COMPOSE_YML)["services"]["prometheus"]
    mounts: list[tuple[Path, str]] = []
    for volume in service.get("volumes", []):
        if not isinstance(volume, str):
            continue
        host, container, *_ = volume.split(":")
        if host.startswith("."):
            mounts.append(((ROOT / host).resolve(), container))
    return mounts


def _resolve(container_path: str, mounts: list[tuple[Path, str]]) -> list[Path]:
    """Host files a ``rule_files`` entry (possibly a glob) can see through the mounts; [] when no mount covers it."""
    found: list[Path] = []
    for host, container in mounts:
        prefix = container.rstrip("/") + "/"
        if not container_path.startswith(prefix) or not host.is_dir():
            continue
        pattern = container_path[len(prefix) :]
        found.extend(
            p for p in sorted(host.rglob("*")) if p.is_file() and fnmatch.fnmatch(str(p.relative_to(host)), pattern)
        )
    return found


def _rule_files_entries() -> list[str]:
    return list(_load(PROMETHEUS_YML)["rule_files"])


def _loaded_rule_files() -> set[Path]:
    mounts = _prometheus_mounts()
    loaded: set[Path] = set()
    for entry in _rule_files_entries():
        loaded.update(p.resolve() for p in _resolve(entry, mounts))
    return loaded


def _rule_files_on_disk() -> set[Path]:
    """Every YAML file under config/monitoring whose top level is a mapping with ``groups`` of rule groups.

    Promtool test files (``tests``), Alertmanager configs (``route``) and Grafana/Loki/Promtail files have another top
    level and are not rule files, so a file moved or added anywhere in the tree is seen.
    """
    found: set[Path] = set()
    for path in sorted(MONITORING.rglob("*.y*ml")):
        data = _load(path)
        groups = data.get("groups") if isinstance(data, dict) else None
        if isinstance(groups, list) and all(isinstance(g, dict) and "rules" in g for g in groups):
            found.add(path.resolve())
    return found


def _rules(paths: set[Path]) -> list[dict]:
    rules: list[dict] = []
    for path in sorted(paths):
        for group in _load(path)["groups"]:
            rules.extend(group.get("rules", []))
    return rules


def test_every_rule_file_in_the_monitoring_tree_is_loaded_by_prometheus():
    unloaded = sorted(str(p.relative_to(ROOT)) for p in _rule_files_on_disk() - _loaded_rule_files())
    assert not unloaded, (
        "rule files nobody evaluates (not matched by rule_files in prometheus.yml through a compose mount of the "
        f"prometheus service): {unloaded}"
    )


@pytest.mark.parametrize("entry", _rule_files_entries())
def test_every_rule_files_entry_reaches_a_file_through_a_mount(entry: str):
    # A rule_files path that matches nothing is not an error in Prometheus; it silently loads nothing.
    assert _resolve(entry, _prometheus_mounts()), (
        f"rule_files entry {entry!r} matches no file through the prometheus service's mounts in docker-compose.yml"
    )


def test_every_recorded_series_an_alert_reads_is_defined_by_a_loaded_rule():
    loaded = _rules(_loaded_rule_files())
    recorded = {rule["record"] for rule in loaded if "record" in rule}
    missing: dict[str, list[str]] = {}
    for rule in loaded:
        if "alert" not in rule:
            continue
        for name in _RECORDED_SERIES.findall(_QUOTED.sub("", str(rule["expr"]))):
            if name not in recorded:
                missing.setdefault(name, []).append(rule["alert"])
    assert not missing, f"alerts read recorded series that no loaded rule defines: {missing}"
