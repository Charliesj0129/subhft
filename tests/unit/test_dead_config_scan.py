from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "dead_config_scan.py"
    spec = importlib.util.spec_from_file_location("dead_config_scan", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _details(findings, category: str) -> list[str]:
    return sorted(
        f.detail.split(":")[0] if category == "dead_config_key" else f.file for f in findings if f.category == category
    )


# ---------------------------------------------------------------------------------------- dead keys

KEYS_YAML = """\
limits:
  read_by_literal: 1
  read_by_attribute: 2
  read_by_field: 3
  read_by_keyword: 4
  never_read: 5
  limit_root_TMF: 6
  ignored_list: [1, 2]
strategies:
  - id: a
    param_never_read: 7
    param_read_by_literal: 8
"""

KEYS_SRC = """
import msgspec


class Cfg(msgspec.Struct):
    read_by_field: int = 0


def load(cfg, make):
    a = cfg["read_by_literal"]
    b = cfg.read_by_attribute
    c = make(read_by_keyword=1)
    d = cfg.get("limit_root_" + "TMF")
    return a, b, c, d, cfg["param_read_by_literal"], cfg["id"]
"""


@pytest.fixture
def keys_tree(tmp_path: Path) -> Path:
    _write(tmp_path, "config/env/prod/limits.yaml", KEYS_YAML)
    _write(tmp_path, "src/hft_platform/loader.py", KEYS_SRC)
    return tmp_path


def test_a_key_nothing_spells_is_dead_and_the_others_are_not(keys_tree):
    findings = mod.find_dead_keys(keys_tree)

    assert sorted(f.detail.split(":")[0] for f in findings) == [
        "limits.ignored_list",
        "limits.never_read",
        "strategies.param_never_read",
    ]


def test_a_dead_key_is_reported_with_its_file_and_line(keys_tree):
    findings = mod.find_dead_keys(keys_tree)

    never = next(f for f in findings if f.detail.startswith("limits.never_read"))
    assert (never.file, never.line) == ("config/env/prod/limits.yaml", 6)


def test_a_key_in_a_family_read_by_prefix_counts_as_read(keys_tree):
    findings = mod.find_dead_keys(keys_tree)

    assert all("limit_root_TMF" not in f.detail for f in findings)


def test_a_short_underscore_string_is_not_a_prefix(keys_tree):
    _write(keys_tree, "src/hft_platform/other.py", 'X = "a_"\n')
    _write(keys_tree, "config/env/prod/more.yaml", "a_secret: 1\n")

    findings = mod.find_dead_keys(keys_tree)

    assert any(f.detail.startswith("a_secret") for f in findings)


def test_only_the_requested_yaml_glob_is_checked(keys_tree):
    _write(keys_tree, "config/other/extra.yaml", "orphan_key: 1\n")

    assert all("orphan_key" not in f.detail for f in mod.find_dead_keys(keys_tree))
    assert any("orphan_key" in f.detail for f in mod.find_dead_keys(keys_tree, "config/other/*.yaml"))


def test_a_yaml_file_that_does_not_parse_or_is_empty_is_skipped(keys_tree):
    _write(keys_tree, "config/env/prod/broken.yaml", "a: [unclosed\n")
    _write(keys_tree, "config/env/prod/empty.yaml", "")

    assert {f.file for f in mod.find_dead_keys(keys_tree)} == {"config/env/prod/limits.yaml"}


# ----------------------------------------------------------------------------- unreferenced files


@pytest.fixture
def files_tree(tmp_path: Path) -> Path:
    _write(
        tmp_path,
        "src/hft_platform/app.py",
        'A = "config/used.yaml"\nB = f"config/env/{mode}/main.yaml"\nC = "solo.yaml"\n',
    )
    _write(tmp_path, "config/used.yaml")
    _write(tmp_path, "config/orphan.yaml")
    _write(tmp_path, "config/env/prod/main.yaml")
    _write(tmp_path, "config/solo.yaml")
    return tmp_path


def test_a_config_file_no_reader_names_is_unreferenced(files_tree):
    findings = mod.find_unreferenced_files(files_tree)

    assert sorted(f.file for f in findings) == ["config/orphan.yaml"]


def test_a_path_template_with_a_placeholder_matches_the_file(files_tree):
    findings = mod.find_unreferenced_files(files_tree)

    assert "config/env/prod/main.yaml" not in {f.file for f in findings}


def test_a_bare_name_counts_only_while_it_is_unique_among_config_files(files_tree):
    _write(files_tree, "config/env/prod/solo.yaml")  # the name is now shared

    findings = mod.find_unreferenced_files(files_tree)

    assert {"config/solo.yaml", "config/env/prod/solo.yaml"} <= {f.file for f in findings}


def test_a_config_file_mentioning_another_by_bare_name_is_not_a_loader(files_tree):
    _write(files_tree, "config/env/prod/notes.yaml", "# see risk.yaml\nx: 1\n")
    _write(files_tree, "config/risk.yaml")

    findings = mod.find_unreferenced_files(files_tree)

    assert "config/risk.yaml" in {f.file for f in findings}


def test_a_file_that_names_itself_is_not_its_own_reader(files_tree):
    _write(files_tree, "config/selfish.yaml", "# config/selfish.yaml\nx: 1\n")

    findings = mod.find_unreferenced_files(files_tree)

    assert "config/selfish.yaml" in {f.file for f in findings}


def test_a_glob_with_a_single_literal_segment_in_code_loads_nothing_in_particular(files_tree):
    _write(files_tree, "src/hft_platform/broad.py", 'ALL = glob("**/*.yaml")\nMORE = glob("config/*.yaml")\n')

    findings = mod.find_unreferenced_files(files_tree)

    assert "config/orphan.yaml" in {f.file for f in findings}


def test_a_workflow_path_filter_with_literal_segments_is_not_a_loader(files_tree):
    _write(files_tree, ".github/workflows/ci.yml", "on:\n  push:\n    paths: ['config/shards/*.yaml']\n")
    _write(files_tree, "config/shards/z.yaml")

    findings = mod.find_unreferenced_files(files_tree)

    assert "config/shards/z.yaml" in {f.file for f in findings}


@pytest.mark.parametrize("token", ["env/prod/partial.yaml", "prod/partial.yaml"])
def test_a_partial_path_in_code_matches_the_file_under_it(files_tree, token):
    _write(files_tree, "src/hft_platform/partial.py", f'P = "{token}"\n')
    _write(files_tree, "config/env/prod/partial.yaml")

    findings = mod.find_unreferenced_files(files_tree)

    assert "config/env/prod/partial.yaml" not in {f.file for f in findings}


def test_an_over_broad_glob_in_a_workflow_loads_nothing(files_tree):
    _write(files_tree, ".github/workflows/ci.yml", "on:\n  push:\n    paths: ['**/*.yaml', 'config/**/*.yaml']\n")

    findings = mod.find_unreferenced_files(files_tree)

    assert "config/orphan.yaml" in {f.file for f in findings}


def test_a_loader_glob_with_two_literal_segments_matches_its_files(files_tree):
    _write(files_tree, "src/hft_platform/globber.py", 'FILES = glob("config/shards/*.yaml")\n')
    _write(files_tree, "config/shards/a.yaml")

    findings = mod.find_unreferenced_files(files_tree)

    assert "config/shards/a.yaml" not in {f.file for f in findings}


def test_a_container_path_in_a_config_file_matches_by_its_last_two_parts(files_tree):
    _write(files_tree, "config/monitoring/prometheus.yml", "rule_files:\n  - /etc/prometheus/alerts/shared.yaml\n")
    _write(files_tree, "config/alerts/shared.yaml")

    findings = mod.find_unreferenced_files(files_tree)

    assert "config/alerts/shared.yaml" not in {f.file for f in findings}


def test_examples_and_monitoring_are_not_reported_as_unreferenced(files_tree):
    _write(files_tree, "config/symbols.examples/minimal.list")
    _write(files_tree, "config/monitoring/dashboards/x.json", "{}")
    _write(files_tree, "config/agent_system/prompt.md")

    findings = mod.find_unreferenced_files(files_tree)

    assert [f.file for f in findings] == ["config/orphan.yaml"]


# ---------------------------------------------------------------------------- Prometheus rule files

PROM_YML = "rule_files:\n  - /etc/prometheus/alerts/rules.yaml\n"
GROUPS = "groups:\n  - name: g\n    rules:\n      - alert: A\n        expr: up == 0\n      - alert: B\n        expr: up == 1\n"
COMPOSE = """\
services:
  prometheus:
    volumes:
      - ./config/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./config/monitoring/alerts:/etc/prometheus/alerts:ro
  other:
    volumes:
      - ./config/monitoring/alerts:/x:ro
"""


@pytest.fixture
def prom_tree(tmp_path: Path) -> Path:
    _write(tmp_path, "config/monitoring/prometheus.yml", PROM_YML)
    _write(tmp_path, "config/monitoring/alerts/rules.yaml", GROUPS)
    _write(tmp_path, "config/monitoring/alerts/extra.yml", GROUPS)
    _write(tmp_path, "config/monitoring/recording_rules/slo.yaml", GROUPS)
    _write(tmp_path, "config/monitoring/alerts/rules_test.yaml", "rule_files: [rules.yaml]\ntests: []\n")
    _write(tmp_path, "config/monitoring/alerts/alertmanager.yml", "route: {}\n")
    _write(tmp_path, "docker-compose.yml", COMPOSE)
    return tmp_path


def test_a_rule_file_prometheus_does_not_list_never_runs(prom_tree):
    findings = mod.find_unloaded_rule_files(prom_tree)

    by_file = {f.file: f.detail for f in findings}
    assert "no rule_files entry matches it" in by_file["config/monitoring/alerts/extra.yml"]
    assert "2 rule(s)" in by_file["config/monitoring/alerts/extra.yml"]


def test_a_rule_file_in_an_unmounted_directory_never_runs(prom_tree):
    findings = mod.find_unloaded_rule_files(prom_tree)

    slo = next(f for f in findings if f.file == "config/monitoring/recording_rules/slo.yaml")
    assert "config/monitoring/recording_rules is not mounted" in slo.detail


def test_the_loaded_rule_file_and_non_rule_yaml_are_not_reported(prom_tree):
    findings = mod.find_unloaded_rule_files(prom_tree)

    assert sorted(f.file for f in findings) == [
        "config/monitoring/alerts/extra.yml",
        "config/monitoring/recording_rules/slo.yaml",
    ]


def test_a_glob_in_rule_files_matches_through_the_mount(prom_tree):
    _write(prom_tree, "config/monitoring/prometheus.yml", "rule_files:\n  - /etc/prometheus/alerts/*.y*ml\n")

    findings = mod.find_unloaded_rule_files(prom_tree)

    assert [f.file for f in findings] == ["config/monitoring/recording_rules/slo.yaml"]


def test_a_service_that_does_not_mount_prometheus_yml_is_not_judged(prom_tree):
    findings = mod.find_unloaded_rule_files(prom_tree)

    assert all("service other" not in f.detail for f in findings)


def test_the_long_form_volume_syntax_is_understood(prom_tree):
    _write(
        prom_tree,
        "docker-compose.yml",
        "services:\n  prometheus:\n    volumes:\n"
        "      - type: bind\n        source: ./config/monitoring/prometheus.yml\n        target: /etc/prometheus/prometheus.yml\n"
        "      - type: bind\n        source: ./config/monitoring/alerts\n        target: /etc/prometheus/alerts\n",
    )

    findings = mod.find_unloaded_rule_files(prom_tree)

    assert "config/monitoring/alerts/rules.yaml" not in {f.file for f in findings}
    assert "config/monitoring/alerts/extra.yml" in {f.file for f in findings}


def test_each_compose_file_is_judged_on_its_own_mounts(prom_tree):
    _write(
        prom_tree,
        "docker-compose.prod.yml",
        "services:\n  prometheus:\n    volumes:\n      - ./config/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml\n",
    )

    findings = mod.find_unloaded_rule_files(prom_tree)

    prod = [f for f in findings if "docker-compose.prod.yml" in f.detail]
    assert "config/monitoring/alerts/rules.yaml" in {f.file for f in prod}  # its directory is not mounted there
    assert all("is not mounted" in f.detail for f in prod)


def test_no_rule_files_means_nothing_to_report(tmp_path):
    assert mod.find_unloaded_rule_files(tmp_path) == []


# ------------------------------------------------------------------------------------------- main


def test_report_mode_exits_zero_and_writes_the_json(keys_tree, capsys):
    out = keys_tree / "report"

    code = mod.main(["--root", str(keys_tree), "--output-dir", str(out)])

    assert code == 0
    payload = json.loads((out / "dead_config_report.json").read_text(encoding="utf-8"))
    assert payload["summary"]["dead_config_key"] == 3
    assert "limits.never_read" in capsys.readouterr().out


def test_strict_mode_exits_one_when_anything_is_found(keys_tree):
    assert mod.main(["--root", str(keys_tree), "--output-dir", str(keys_tree / "r"), "--strict"]) == 1


def test_strict_mode_exits_zero_on_a_clean_tree(tmp_path):
    assert mod.main(["--root", str(tmp_path), "--output-dir", str(tmp_path / "r"), "--strict"]) == 0


def test_the_real_repository_scans_without_error():
    findings, summary = mod.evaluate(Path(__file__).resolve().parents[2])

    assert set(summary) == set(mod.CATEGORIES)
    assert {f.category for f in findings} <= set(mod.CATEGORIES)
