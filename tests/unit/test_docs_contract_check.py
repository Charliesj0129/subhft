from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "docs_contract_check.py"
    spec = importlib.util.spec_from_file_location("docs_contract_check", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hft")
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command")
    risk = sub.add_parser("risk")
    risk_sub = risk.add_subparsers(dest="risk_cmd")
    halt = risk_sub.add_parser("halt")
    halt.add_argument("--reason", required=True)
    halt.add_argument("--dry-run", action="store_true")
    risk_sub.add_parser("resume")
    run = sub.add_parser("run")
    run.add_argument("mode", choices=["sim", "live"])
    run.add_argument("--symbols")
    return parser


def _spans(text: str):
    return list(mod.iter_code_spans(text))


def _cats(findings) -> list[str]:
    return [f.category for f in findings]


# ── CLI ──


def test_an_unknown_cli_subcommand_is_reported_with_the_defined_ones():
    found = mod.check_cli(_spans("```bash\nhft admin status --broker x\n```\n"), _parser(), "d.md")

    assert _cats(found) == ["cli_command"]
    assert "no subcommand `admin`" in found[0].detail and "risk" in found[0].detail
    assert found[0].line == 2


def test_flags_after_an_unknown_subcommand_are_not_reported_against_the_parent():
    found = mod.check_cli(_spans("`hft admin status --verbose --full`"), _parser(), "d.md")

    assert _cats(found) == ["cli_command"]  # no cascade of cli_flag


def test_a_real_command_with_real_flags_and_a_quoted_value_is_clean():
    text = '`hft risk halt --reason "manual stop, see ticket" --dry-run`\n`uv run hft run live --symbols=TMFD6`\n'

    assert mod.check_cli(_spans(text), _parser(), "d.md") == []


def test_an_unknown_flag_on_a_known_command_is_reported():
    found = mod.check_cli(_spans("`hft risk halt --reason x --force`"), _parser(), "d.md")

    assert _cats(found) == ["cli_flag"]
    assert "--force" in found[0].detail and "hft risk halt" in found[0].detail


def test_an_unambiguous_abbreviation_of_a_long_flag_is_accepted_and_a_stranger_is_not():
    accepted = mod.check_cli(_spans("`hft risk halt --rea x --dry`\n"), _parser(), "d.md")
    rejected = mod.check_cli(_spans("`hft risk halt --zzz`\n"), _parser(), "d.md")

    assert accepted == []
    assert _cats(rejected) == ["cli_flag"]


def test_a_flag_value_is_not_read_as_a_subcommand():
    text = "`hft --help`\n`hft risk halt --reason resume`\n`hft --config prod risk resume`"

    assert mod.check_cli(_spans(text), _parser(), "d.md") == []


def test_container_and_package_names_that_start_with_hft_are_not_commands():
    text = "`docker exec hft-engine ls`\n`python -m hft_platform.cli`\n`cd /opt/hft/run`\n"

    assert mod.check_cli(_spans(text), _parser(), "d.md") == []


def test_placeholders_stop_command_resolution():
    assert mod.check_cli(_spans("`hft <group> <sub>`\n`hft $CMD run`"), _parser(), "d.md") == []


# ── make ──


def test_makefile_targets_ignore_recipes_variables_and_phony_lists():
    text = ".PHONY: a b\nVAR := 1\ncheck: lint\n\techo hi\nlint test: ## both\n\t@true\n"

    assert mod.makefile_targets(text) == {"check", "lint", "test"}


def test_a_make_target_without_a_rule_is_reported_and_variables_are_skipped():
    targets = {"check", "test-file"}
    text = "`make check`\n`make FILE=x.py test-file`\n`make release-readiness-check`\n`make -j4 check`\n"

    found = mod.check_make(_spans(text), targets, "d.md")

    assert [f.detail for f in found] == ["`make release-readiness-check` has no rule in the Makefile"]
    assert found[0].line == 3


def test_a_variable_assignment_before_the_target_does_not_hide_a_missing_target():
    found = mod.check_make(_spans("`make FILE=x.py ghost-target`\n"), {"check"}, "d.md")

    assert [f.detail for f in found] == ["`make ghost-target` has no rule in the Makefile"]


# ── metrics ──


def test_metric_selectors_are_checked_and_prefix_mistakes_are_explained():
    defined = {"order_rtt_ms", "feed_events_total"}
    text = (
        "```promql\n"
        'rate(hft_feed_events_total{job="hft-engine"}[5m])\n'
        "histogram_quantile(0.99, rate(order_rtt_ms_bucket[5m]))\n"
        "rate(feed_events_total[5m])\n"
        "rate(no_such_metric[5m])\n"
        "```\n"
    )

    found = mod.check_metrics(_spans(text), defined, "d.md")

    assert [f.line for f in found] == [2, 5]
    assert "defined without the `hft_` prefix" in found[0].detail
    assert "`no_such_metric` is not defined" in found[1].detail


def test_brace_expansion_json_and_logql_are_not_metrics():
    text = (
        "`hft_quote_conn_{logged_in,state}` are written\n"
        "```bash\nawk 'BEGIN{n=0} {print}' f | jq '.x'\n```\n"
        '```logql\nsum(count_over_time({job="e"} | json [1h]))\n```\n'
    )

    assert mod.check_metrics(_spans(text), set(), "d.md") == []


def test_external_exporter_metrics_are_not_reported():
    text = '`node_filesystem_avail_bytes{mountpoint="/"}`\n`up{job="x"}`\n`process_cpu_seconds_total[5m]`\n'

    assert mod.check_metrics(_spans(text), set(), "d.md") == []


def test_a_bare_hft_prefixed_inline_name_is_checked_but_the_package_name_is_not():
    found = mod.check_metrics(_spans("`hft_wal_files_pending` and `hft_platform`"), set(), "d.md")

    assert [f.detail.split("`")[1] for f in found] == ["hft_wal_files_pending"]


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "hft_platform").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "docs" / "runbooks").mkdir(parents=True)
    (root / "config" / "monitoring" / "alerts").mkdir(parents=True)
    (root / "config" / "monitoring" / "recording_rules").mkdir(parents=True)
    (root / "Makefile").write_text("check:\n\ttrue\n", encoding="utf-8")
    return root


def test_defined_metrics_come_from_code_textfile_scripts_and_recording_rules(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "src" / "hft_platform" / "m.py").write_text(
        'a = _pn("queue_depth")\nb = Counter("orders_total", "help")\nc = Gauge(name_var)\n', encoding="utf-8"
    )
    (root / "scripts" / "probe.sh").write_text("echo '# TYPE disk_free_bytes gauge'\n", encoding="utf-8")
    (root / "config" / "monitoring" / "recording_rules" / "slo.yaml").write_text(
        "groups:\n- name: g\n  rules:\n  - record: slo:orders:ratio\n    expr: x\n", encoding="utf-8"
    )

    assert mod.collect_defined_metrics(root) == {"queue_depth", "orders_total", "disk_free_bytes", "slo:orders:ratio"}


# ── paths and links ──


def test_a_missing_repo_path_is_reported_and_placeholders_and_runtime_dirs_are_not(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "scripts" / "real.py").write_text("", encoding="utf-8")
    text = (
        "`scripts/real.py`\n`scripts/gone.py:12`\n`scripts/*.py`\n`docs/<name>.md`\n"
        "`research/data/raw/x`\n`scripts/`\n```bash\ncat ${ROOT}/scripts/x.py\n```\n"
    )

    found = mod.check_paths(_spans(text), root, "d.md")

    assert [(f.line, f.detail) for f in found] == [(2, "`scripts/gone.py` does not exist")]


def test_a_gitignored_path_is_treated_as_generated_not_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(mod, "git_ignored", lambda _root, paths: {p for p in paths if p.endswith("contracts.json")})

    found = mod.check_paths(_spans("`config/contracts.json` and `config/gone.yaml`"), root, "d.md")

    assert [f.detail for f in found] == ["`config/gone.yaml` does not exist"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_ignored_asks_git_by_name_only(tmp_path: Path):
    root = _repo(tmp_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".gitignore").write_text("config/contracts.json\n", encoding="utf-8")

    assert mod.git_ignored(root, ["config/contracts.json", "config/other.json"]) == {"config/contracts.json"}


def test_relative_links_are_resolved_from_the_document_and_urls_and_anchors_are_skipped(tmp_path: Path):
    root = _repo(tmp_path)
    doc = root / "docs" / "runbooks" / "a.md"
    (root / "docs" / "runbooks" / "b.md").write_text("", encoding="utf-8")
    (root / "docs" / "guide.md").write_text("", encoding="utf-8")
    text = (
        "[ok](b.md) [up](../guide.md#sec) [root](/docs/guide.md) [url](https://x.test/y) [anchor](#top) "
        "[gone](missing.md) [old](../runbooks.md#9-service)\n"
        "```md\n[inside fence](nothing.md)\n```\n"
        "`[inline code](nothing.md)`\n"
    )

    found = mod.check_links(mod.iter_prose_links(text), doc, root, "docs/runbooks/a.md")

    assert [f.detail for f in found] == [
        "link target `missing.md` does not exist",
        "link target `../runbooks.md#9-service` does not exist",
    ]


# ── alerts ──


def test_alert_runbook_urls_and_expression_metrics_are_checked(tmp_path: Path):
    root = _repo(tmp_path)
    (root / "docs" / "runbooks" / "Good.md").write_text("", encoding="utf-8")
    (root / "config" / "monitoring" / "alerts" / "rules.yaml").write_text(
        "groups:\n"
        "- name: g\n"
        "  rules:\n"
        "  - alert: Good\n"
        "    expr: rate(queue_depth_total[5m]) > 0 and on(job) up{job='x'} == 1\n"
        "    annotations: {runbook_url: 'docs/runbooks/Good.md#sec'}\n"
        "  - alert: Bad\n"
        '    expr: sum by (job) (rate(ghost_metric{kind="a"}[5m])) / ignoring(x) queue_depth > 0.5\n'
        "    annotations: {runbook_url: 'docs/runbooks/Gone.md'}\n"
        "  - alert: External\n"
        "    expr: node_load1 > 4\n"
        "    annotations: {runbook_url: 'https://example.test/rb'}\n",
        encoding="utf-8",
    )
    (root / "config" / "monitoring" / "alerts" / "alertmanager.yml").write_text(
        "route: {receiver: x}\n", encoding="utf-8"
    )

    found = mod.check_alert_files(root, {"queue_depth"})

    assert sorted((f.category, f.detail.split(":")[0]) for f in found) == [
        ("alert_metric", "alert Bad"),
        ("runbook_url", "alert Bad"),
    ]
    assert any("ghost_metric" in f.detail for f in found)


def test_alert_test_fixtures_and_alertmanager_config_are_not_read_as_alert_rules(tmp_path: Path):
    root = _repo(tmp_path)
    rule = "groups:\n- name: g\n  rules:\n  - alert: A\n    expr: ghost_metric > 0\n"
    alerts = root / "config" / "monitoring" / "alerts"
    (alerts / "rules_test.yaml").write_text(rule, encoding="utf-8")
    (alerts / "alertmanager.prod.yml").write_text(rule, encoding="utf-8")

    assert mod.check_alert_files(root, set()) == []

    (alerts / "rules.yaml").write_text(rule, encoding="utf-8")

    assert _cats(mod.check_alert_files(root, set())) == ["alert_metric"]


def test_promql_names_skip_functions_labels_numbers_and_keywords():
    names = mod._promql_metric_names(
        'sum by (job, instance) (rate(a_total{x="y"}[5m])) / on(job) group_left b > bool 0.5 offset 5m and c'
    )

    assert names == {"a_total", "b", "c"}


# ── whole run and CLI ──


def _doc_repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    (root / "src" / "hft_platform" / "m.py").write_text('a = _pn("queue_depth")\n', encoding="utf-8")
    (root / "docs" / "runbooks" / "r.md").write_text(
        "# R\n\n`hft admin x`\n`make nothing`\n`hft_ghost_total`\n`scripts/gone.sh`\n[l](gone.md)\n", encoding="utf-8"
    )
    return root


def test_run_checks_reports_every_category_once(tmp_path: Path):
    root = _doc_repo(tmp_path)

    found = mod.run_checks(root, ["docs/runbooks"], parser=_parser())

    assert sorted(_cats(found)) == ["cli_command", "make_target", "metric", "path", "path"]
    assert mod.summarize(found)["path"] == 2


def test_doc_roots_are_scanned_recursively(tmp_path: Path):
    root = _repo(tmp_path)
    nested = root / "docs" / "runbooks" / "sub" / "deeper"
    nested.mkdir(parents=True)
    (nested / "n.md").write_text("`scripts/gone.sh`\n", encoding="utf-8")

    found = mod.run_checks(root, ["docs/runbooks"], parser=_parser(), include_alerts=False)

    assert [(f.category, f.file) for f in found] == [("path", "docs/runbooks/sub/deeper/n.md")]


def test_report_mode_exits_zero_and_strict_mode_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    root = _doc_repo(tmp_path)
    original = mod.run_checks
    monkeypatch.setattr(mod, "run_checks", lambda r, d, **kw: original(r, d, parser=_parser()))
    out_dir = tmp_path / "out"

    rc_report = mod.main(["--root", str(root), "--docs", "docs/runbooks", "--output-dir", str(out_dir)])
    printed = capsys.readouterr().out
    rc_strict = mod.main(["--root", str(root), "--docs", "docs/runbooks", "--output-dir", "", "--strict"])

    assert rc_report == 0 and rc_strict == 1
    assert "5 finding(s)" in printed
    payload = json.loads((out_dir / "docs_contract_report.json").read_text(encoding="utf-8"))
    assert payload["total"] == 5 and payload["counts"]["cli_command"] == 1


def test_a_clean_tree_is_clean_in_strict_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _repo(tmp_path)
    (root / "docs" / "runbooks" / "ok.md").write_text("`hft risk resume`\n`make check`\n", encoding="utf-8")
    original = mod.run_checks
    monkeypatch.setattr(mod, "run_checks", lambda r, d, **kw: original(r, d, parser=_parser()))

    assert mod.main(["--root", str(root), "--docs", "docs/runbooks", "--output-dir", "", "--strict"]) == 0
