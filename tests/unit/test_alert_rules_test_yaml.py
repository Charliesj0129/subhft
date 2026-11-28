"""Static guards for config/monitoring/alerts/rules_test.yaml.

``promtool test rules`` is not part of ``make test`` (it needs the Prometheus image), so a broken fixture file can sit
unnoticed. On 2026-11-03 a bad merge left two ``summary`` and two ``description`` keys in one ``exp_annotations``
mapping of that file: the expectation of ``UniverseOnLastGoodFallback`` was pasted into the one of
``FrontMonthWatchStalled``, and the case it came from kept no expectation at all. promtool parses the file strictly and
refuses a repeated key, so none of the 75 groups ran from then on. PyYAML keeps the last value of a repeated key without
a word, which is why the other tests of this directory could not see it.

These checks need no promtool. They cover what the YAML alone decides:

* no mapping of the rule files or of the fixture file repeats a key;
* every ``alertname`` a case asserts is an alert one of the loaded rule files defines;
* every expected annotation set has the keys of its rule, and the text of the rule when the rule's template holds no
  ``{{ }}`` placeholder.

What the PromQL does with the input series still needs ``make alert-rules-test``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ALERTS_DIR = Path(__file__).resolve().parents[2] / "config" / "monitoring" / "alerts"
RULES_PATH = ALERTS_DIR / "rules.yaml"
SLO_RULES_PATH = ALERTS_DIR / "slo_recording_rules.yaml"
RULES_TEST_PATH = ALERTS_DIR / "rules_test.yaml"

_MERGE_TAG = "tag:yaml.org,2002:merge"


class DuplicateKeyError(yaml.YAMLError):
    """A YAML mapping repeats a key."""


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping repeating a key, as promtool's strict parser does."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        first_line: dict[Any, int] = {}
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                continue
            key = self.construct_object(key_node, deep=True)
            line = key_node.start_mark.line + 1
            if key in first_line:
                raise DuplicateKeyError(
                    f"duplicate mapping key {key!r} at line {line} (first at line {first_line[key]})"
                )
            first_line[key] = line
        return super().construct_mapping(node, deep=deep)


def _load_strict_text(text: str) -> Any:
    loader = _StrictLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def _load_strict(path: Path) -> Any:
    return _load_strict_text(path.read_text(encoding="utf-8"))


def _load_plain(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _alerts_by_name(test_doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every alert defined by a rule file the test file loads, by name."""
    alerts: dict[str, dict[str, Any]] = {}
    for rule_file in test_doc["rule_files"]:
        for group in _load_plain(ALERTS_DIR / rule_file)["groups"]:
            for rule in group["rules"]:
                if "alert" in rule:
                    alerts[rule["alert"]] = rule
    return alerts


def _cases(test_doc: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """(group name, alert_rule_test case) for every alert case of the fixture file."""
    for index, group in enumerate(test_doc["tests"]):
        for case in group.get("alert_rule_test") or []:
            yield f"#{index} {group.get('name', '')}", case


def _unknown_alertnames(test_doc: dict[str, Any], alerts: dict[str, dict[str, Any]]) -> list[str]:
    return [f"{where}: {case['alertname']}" for where, case in _cases(test_doc) if case["alertname"] not in alerts]


def _annotation_key_problems(test_doc: dict[str, Any], alerts: dict[str, dict[str, Any]]) -> list[str]:
    """Expected annotation sets whose keys differ from the rule's; promtool compares the whole set."""
    problems: list[str] = []
    for where, case in _cases(test_doc):
        rule = alerts.get(case["alertname"])
        if rule is None:
            continue
        rule_keys = set(rule.get("annotations") or {})
        for expected in case.get("exp_alerts") or []:
            expected_keys = set(expected.get("exp_annotations") or {})
            if expected_keys != rule_keys:
                problems.append(
                    f"{where}: {case['alertname']} expects {sorted(expected_keys)}, rule has {sorted(rule_keys)}"
                )
    return problems


def _placeholder_free_annotations(
    test_doc: dict[str, Any], alerts: dict[str, dict[str, Any]]
) -> Iterator[tuple[str, str, str, str]]:
    """(where, key, expected text, rule text) for each expected annotation whose rule template holds no placeholder."""
    for where, case in _cases(test_doc):
        rule = alerts.get(case["alertname"])
        if rule is None:
            continue
        template = rule.get("annotations") or {}
        for expected in case.get("exp_alerts") or []:
            for key, text in (expected.get("exp_annotations") or {}).items():
                rule_text = template.get(key)
                if rule_text is not None and "{{" not in str(rule_text):
                    yield f"{where}: {case['alertname']}", key, str(text), str(rule_text)


@pytest.mark.parametrize("path", [RULES_PATH, SLO_RULES_PATH, RULES_TEST_PATH], ids=lambda path: path.name)
def test_alert_rule_files_have_no_duplicate_mapping_keys_when_loaded_strictly(path: Path) -> None:
    try:
        doc = _load_strict(path)
    except DuplicateKeyError as exc:
        pytest.fail(f"{path.name}: {exc}")
    assert isinstance(doc, dict)
    assert "groups" in doc or "tests" in doc


def test_strict_loader_rejects_a_key_repeated_in_one_mapping() -> None:
    text = "exp_annotations:\n  summary: a\n  description: b\n  summary: c\n"
    with pytest.raises(DuplicateKeyError, match=r"'summary' at line 4 \(first at line 2\)"):
        _load_strict_text(text)


def test_strict_loader_accepts_the_same_key_in_sibling_mappings() -> None:
    loaded = _load_strict_text("- {summary: a}\n- {summary: b}\n")
    assert loaded == [{"summary": "a"}, {"summary": "b"}]


def test_rules_test_alertnames_all_exist_in_the_loaded_rule_files() -> None:
    test_doc = _load_plain(RULES_TEST_PATH)
    alerts = _alerts_by_name(test_doc)
    assert alerts, "no alert found in the rule files the fixture loads"
    assert list(_cases(test_doc)), "no alert_rule_test case found in the fixture file"
    assert _unknown_alertnames(test_doc, alerts) == []


def test_unknown_alertnames_names_a_case_asserting_a_missing_alert() -> None:
    test_doc = {"tests": [{"name": "g", "alert_rule_test": [{"alertname": "Gone", "eval_time": "1m"}]}]}
    assert _unknown_alertnames(test_doc, {"Present": {}}) == ["#0 g: Gone"]


def test_rules_test_expected_annotations_match_rule_templates_without_placeholders() -> None:
    test_doc = _load_plain(RULES_TEST_PATH)
    alerts = _alerts_by_name(test_doc)
    compared = list(_placeholder_free_annotations(test_doc, alerts))
    assert compared, "no placeholder-free annotation was compared; the guard is vacuous"
    mismatched = [
        f"{where} {key}: {expected!r} != {rule_text!r}"
        for where, key, expected, rule_text in compared
        if expected != rule_text
    ]
    assert mismatched == []
    assert _annotation_key_problems(test_doc, alerts) == []


def test_annotation_guard_reports_text_drift_and_a_missing_key() -> None:
    alerts = {"A": {"annotations": {"summary": "plain", "description": "has {{ $value }}", "runbook_url": "r"}}}
    test_doc = {
        "tests": [
            {
                "name": "g",
                "alert_rule_test": [
                    {
                        "alertname": "A",
                        "eval_time": "1m",
                        "exp_alerts": [{"exp_annotations": {"summary": "other", "description": "rendered 3"}}],
                    }
                ],
            }
        ]
    }
    compared = list(_placeholder_free_annotations(test_doc, alerts))
    assert compared == [("#0 g: A", "summary", "other", "plain")]
    assert _annotation_key_problems(test_doc, alerts) == [
        "#0 g: A expects ['description', 'summary'], rule has ['description', 'runbook_url', 'summary']"
    ]
