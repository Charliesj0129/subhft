from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "dead_metrics_scan.py"
    spec = importlib.util.spec_from_file_location("dead_metrics_scan", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()

METRICS_PY = """
from prometheus_client import Counter, Gauge, Histogram


def _pn(name):
    return name


class MetricsRegistry:
    def __init__(self):
        self.used = Counter(_pn("used_total"), "used by a service")
        self.dead = Counter(_pn("dead_total"), "nothing touches it")
        self.dead_alerted = Counter(_pn("dead_alerted_total"), "only an alert reads it")
        self.dead_latency = Histogram(_pn("dead_latency_ns"), "no one observes it")
        self.dyn = Gauge(_pn("dyn_gauge"), "reached through getattr")
        self.stringy = Gauge(_pn("stringy_gauge"), "named in a table")
        self.helper_used = Counter(_pn("helper_used_total"), "bumped inside the registry")
        self.not_a_metric = dict()

    def bump(self):
        self.helper_used.inc()
"""

SERVICE_PY = """
def run(metrics):
    metrics.used.labels(kind="x").inc()
    gauge = getattr(metrics, "dyn", None)
    if gauge is not None:
        gauge.set(1)


TABLE = {"stringy": "stringy"}
"""

RULES_YAML = """\
groups:
  - name: g
    rules:
      - alert: DeadAlert
        expr: increase(dead_alerted_total[1h]) > 0
      - alert: LiveAlert
        expr: rate(used_total[5m]) > 0
      - alert: LatencyAlert
        expr: histogram_quantile(0.99, rate(dead_latency_ns_bucket[5m])) > 1
      - alert: NearMiss
        expr: increase(my_dead_alerted_total[1h]) + dead_alerted_total_other > 0
  # - alert: CommentedOut
  #   expr: increase(dead_total[1h]) > 0
"""


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    _write(tmp_path, "src/hft_platform/observability/metrics.py", METRICS_PY)
    _write(tmp_path, "src/hft_platform/service.py", SERVICE_PY)
    _write(tmp_path, "config/monitoring/alerts/rules.yaml", RULES_YAML)
    return tmp_path


def _by_category(findings, category: str) -> list[str]:
    return [f.detail.split(" ")[0] for f in findings if f.category == category]


def test_a_metric_nothing_touches_is_reported_as_unreferenced(tree):
    findings, summary = mod.evaluate(tree)

    assert sorted(_by_category(findings, "unreferenced_metric")) == [
        "dead_alerted_total",
        "dead_latency_ns",
        "dead_total",
    ]
    assert summary["defined"] == 7


def test_a_metric_used_inside_the_registry_itself_is_not_dead(tree):
    findings, _ = mod.evaluate(tree)

    assert "helper_used_total" not in " ".join(f.detail for f in findings)


def test_a_metric_reached_only_through_getattr_is_dynamic_not_dead(tree):
    findings, _ = mod.evaluate(tree)

    assert _by_category(findings, "dynamic_only_metric") == ["dyn_gauge", "stringy_gauge"]
    assert "dyn_gauge" not in _by_category(findings, "unreferenced_metric")
    dyn = next(f for f in findings if f.detail.startswith("dyn_gauge"))
    assert "only 1 getattr and 0 string reference(s)" in dyn.detail  # the getattr argument is not counted twice


def test_a_string_literal_in_another_module_counts_as_a_reference_but_a_dynamic_one(tree):
    findings, _ = mod.evaluate(tree)

    stringy = [f for f in findings if f.detail.startswith("stringy_gauge")]
    assert [f.category for f in stringy] == ["dynamic_only_metric"]
    assert "0 getattr and 2 string reference(s)" in stringy[0].detail  # the key and the value


def test_the_name_list_inside_the_registry_is_not_a_reference(tree):
    _write(
        tree,
        "src/hft_platform/observability/metrics.py",
        METRICS_PY + '\n\nNAMES = ["dead", "dead_alerted"]\n',
    )

    findings, _ = mod.evaluate(tree)

    assert "dead_total" in _by_category(findings, "unreferenced_metric")


def test_an_active_alert_on_a_dead_metric_is_reported_with_its_line(tree):
    findings, _ = mod.evaluate(tree)

    alerts = [f for f in findings if f.category == "alert_on_unreferenced_metric"]
    assert sorted(f.detail.split(" uses ")[0] for f in alerts) == ["alert DeadAlert", "alert LatencyAlert"]
    dead = next(f for f in alerts if "DeadAlert" in f.detail)
    assert dead.file == "config/monitoring/alerts/rules.yaml"
    assert dead.line == 4  # the line of ``- alert: DeadAlert``


def test_an_alert_on_a_live_metric_is_not_reported(tree):
    findings, _ = mod.evaluate(tree)

    assert "LiveAlert" not in " ".join(f.detail for f in findings)


def test_a_commented_out_alert_does_not_count(tree):
    findings, _ = mod.evaluate(tree)

    assert "CommentedOut" not in " ".join(f.detail for f in findings)
    dead_total = next(f for f in findings if f.detail.startswith("dead_total "))
    assert "no alert, rule or dashboard" in dead_total.detail


def test_series_suffixes_fold_but_longer_names_do_not_match(tree):
    findings, _ = mod.evaluate(tree)

    latency = [f for f in findings if f.category == "alert_on_unreferenced_metric" and "dead_latency_ns" in f.detail]
    assert [f.detail.split(" uses ")[0] for f in latency] == ["alert LatencyAlert"]  # via _bucket
    near_miss = [f for f in findings if "NearMiss" in f.detail]
    assert near_miss == []  # my_dead_alerted_total / dead_alerted_total_other are other series


def test_a_dashboard_that_reads_a_dead_metric_is_named_in_the_finding(tree):
    _write(tree, "config/monitoring/dashboards/ops.json", '{"expr": "rate(dead_total[5m])"}')

    findings, _ = mod.evaluate(tree)

    dead_total = next(f for f in findings if f.detail.startswith("dead_total "))
    assert "dashboard ops" in dead_total.detail
    assert [f for f in findings if f.category == "alert_on_unreferenced_metric" and "dead_total" in f.detail] == []


def test_a_recording_rule_on_a_dead_metric_is_reported_as_a_rule(tree):
    _write(
        tree,
        "config/monitoring/recording_rules/slo.yaml",
        "groups:\n  - name: slo\n    rules:\n      - record: slo:dead:rate5m\n        expr: rate(dead_total[5m])\n",
    )

    findings, _ = mod.evaluate(tree)

    rules = [f for f in findings if f.category == "alert_on_unreferenced_metric" and "record" in f.detail]
    assert [(f.file, f.detail.split(" uses ")[0]) for f in rules] == [
        ("config/monitoring/recording_rules/slo.yaml", "record slo:dead:rate5m")
    ]


def test_the_exported_name_not_the_attribute_is_what_alerts_are_matched_against(tmp_path):
    _write(
        tmp_path,
        "src/hft_platform/observability/metrics.py",
        'class R:\n    def __init__(self):\n        self.renamed = Counter(_pn("exported_other_total"), "x")\n',
    )
    _write(tmp_path, "src/hft_platform/service.py", "X = 1\n")
    _write(
        tmp_path,
        "config/monitoring/alerts/rules.yaml",
        "groups:\n  - name: g\n    rules:\n      - alert: Exported\n        expr: rate(exported_other_total[1m]) > 0\n"
        "      - alert: Attr\n        expr: rate(renamed[1m]) > 0\n",
    )

    findings, _ = mod.evaluate(tmp_path)

    assert [f.detail.split(" uses ")[0] for f in findings if f.category == "alert_on_unreferenced_metric"] == [
        "alert Exported"
    ]


def test_a_file_that_does_not_parse_is_skipped(tree):
    _write(tree, "src/hft_platform/broken.py", "def (:\n")

    findings, summary = mod.evaluate(tree)

    assert summary["unreferenced_metric"] == 3
    assert findings


def test_report_mode_exits_zero_and_writes_the_json(tree, capsys):
    out = tree / "report"

    code = mod.main(["--root", str(tree), "--output-dir", str(out)])

    assert code == 0
    payload = json.loads((out / "dead_metrics_report.json").read_text(encoding="utf-8"))
    assert payload["summary"]["unreferenced_metric"] == 3
    assert {f["category"] for f in payload["findings"]} == set(mod.CATEGORIES)
    printed = capsys.readouterr().out
    assert "dead_alerted_total" in printed and "dyn_gauge" not in printed  # dynamic rows are opt-in on the console


def test_show_dynamic_lists_the_dynamic_rows(tree, capsys):
    mod.main(["--root", str(tree), "--output-dir", str(tree / "r"), "--show-dynamic"])

    assert "dyn_gauge" in capsys.readouterr().out


def test_strict_mode_exits_one_when_anything_is_found(tree):
    assert mod.main(["--root", str(tree), "--output-dir", str(tree / "r"), "--strict"]) == 1


def test_strict_mode_exits_zero_on_a_clean_tree(tmp_path):
    _write(tmp_path, "src/hft_platform/observability/metrics.py", METRICS_PY)
    _write(
        tmp_path,
        "src/hft_platform/service.py",
        "def run(m):\n    for name in ('used', 'dead', 'dead_alerted', 'dead_latency', 'dyn', 'stringy'):\n"
        "        pass\n    m.used.inc(); m.dead.inc(); m.dead_alerted.inc(); m.dead_latency.observe(1)\n"
        "    m.dyn.set(1); m.stringy.set(1)\n",
    )

    assert mod.main(["--root", str(tmp_path), "--output-dir", str(tmp_path / "r"), "--strict"]) == 0


def test_the_real_registry_parses_and_defines_many_metrics():
    findings, summary = mod.evaluate(Path(__file__).resolve().parents[2])

    assert summary["defined"] > 200
    assert {f.category for f in findings} <= set(mod.CATEGORIES)
