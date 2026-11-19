import subprocess
import sys

import pytest
import yaml

from scripts.check_code_policy import FLOAT_POLICY, ROOT, check_configuration, check_python, check_rust


@pytest.mark.parametrize(
    "source",
    [
        b"#!/usr/bin/env python3\n# coding: utf-8\nvalue = '# noqa'\n",
        b'"""The wire tag starts with #."""\n',
        b"value = 1\n",
    ],
)
def test_accepts_python_syntax_and_hashes_in_strings(source):
    assert list(check_python("example.py", source, b"")) == []


@pytest.mark.parametrize("source", [b"# Workaround\n", b"value = 1  # synchronize later\n"])
def test_rejects_python_comments_in_editable_files(source):
    errors = list(check_python("example.py", source, source))
    assert len(errors) == 1
    assert "HFT-D004" in errors[0]


@pytest.mark.parametrize(
    "directive",
    [b"# noqa: F401", b"# type: ignore", b"# precision-ok: time", b"# nosec", b"# pragma: no cover"],
)
def test_rejects_new_inline_tool_directives(directive):
    source = b"value = 1  " + directive
    errors = list(check_python("example.py", source, b"value = 1"))
    assert len(errors) == 1
    assert "HFT-D005" in errors[0]


def test_existing_directive_cannot_be_reused_on_changed_code():
    old = b"import foo  # noqa: F401\n"
    assert list(check_python("example.py", b"\n" + old, old)) == []
    errors = list(check_python("example.py", b"import bar  # noqa: F401\n", old))
    assert len(errors) == 1
    assert "HFT-D005" in errors[0]


def test_protected_path_rejects_new_comments():
    old = b"# Wire contract\nvalue = 1\n"
    path = "src/hft_platform/contracts/example.py"
    assert list(check_python(path, old, old)) == []
    errors = list(check_python(path, old + b"# another explanation\n", old))
    assert len(errors) == 1
    assert "HFT-D004" in errors[0]


def test_runtime_pytest_detection_blocks_new_production_special_cases():
    source = b"import sys\nif 'pytest' in sys.modules:\n    value = 1\n"
    path = "src/hft_platform/example.py"
    errors = list(check_python(path, source, b""))
    assert len(errors) == 1
    assert "HFT-D002" in errors[0]
    assert list(check_python("tests/example.py", source, b"")) == []


def test_ruff_rule_changes_reject_added_suppression_and_removed_rule():
    old = b'[tool.ruff.lint]\nselect = ["E", "F"]\n'
    new = b'[tool.ruff.lint]\nselect = ["E"]\nignore = ["E501"]\n'
    errors = list(check_configuration("pyproject.toml", new, old))
    assert len(errors) == 2
    assert all("HFT-D005" in error for error in errors)


def test_ruff_config_rejects_new_override_in_child_directory():
    errors = list(check_configuration("src/ruff.toml", b'[lint]\nignore = ["F401"]\n', b""))
    assert "use the root Ruff configuration" in errors[0]


def test_workflow_failure_propagation_blocks_new_continue_on_error():
    old = b"jobs:\n  lint:\n    steps:\n      - name: Lint\n        run: make lint\n"
    new = old + b"        continue-on-error: true\n"
    errors = list(check_configuration(".github/workflows/ci.yml", new, old))
    assert len(errors) == 1
    assert "HFT-D005" in errors[0]
    assert list(check_configuration(".github/workflows/ci.yml", old, new)) == []
    different_command = new.replace(b"make lint", b"make ci")
    assert len(list(check_configuration(".github/workflows/ci.yml", different_command, new))) == 1


def test_policy_fails_closed_if_base_revision_is_missing():
    result = subprocess.run(
        [sys.executable, "-m", "scripts.check_code_policy", "--base", "missing-policy-base-for-test"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "Code policy check could not complete" in result.stderr


def test_rust_lint_preserves_doc_comments_and_string_content():
    source = b'/// API documentation\nfn f() { let s = r#"// literal /* data */"#; }\n'
    assert list(check_rust("lib.rs", source, b"")) == []


def test_rust_lint_rejects_comments_in_code_and_attributes():
    source = b"#[cfg(any(/* note /* nested */ */))]\nfn f() {} // explain\n"
    errors = list(check_rust("lib.rs", source, b""))
    assert len(errors) == 2
    assert all("HFT-D004" in error for error in errors)


@pytest.mark.parametrize("attribute", [b"allow(dead_code)", b"expect(dead_code)", b"cfg_attr(test, allow(dead_code))"])
def test_rust_lint_rejects_new_suppressions(attribute):
    source = b"#[" + attribute + b"]\nfn f() {}\n"
    assert list(check_rust("lib.rs", source, source)) == []
    errors = list(check_rust("lib.rs", source, b""))
    assert len(errors) == 1
    assert "HFT-D005" in errors[0]


def test_github_lint_job_runs_policy_as_a_blocking_step():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_bytes())
    lint = workflow["jobs"]["lint"]
    step = next(step for step in lint["steps"] if "make lint" in step.get("run", ""))
    assert "CODE_POLICY_BASE" in step["env"]
    assert step.get("continue-on-error", False) is False
    assert "if" not in step
    assert lint.get("continue-on-error", False) is False
    assert "if" not in lint


def test_float_policy_rejects_new_known_violations():
    existing = {
        "file": "src/hft_platform/risk/rule.py",
        "code": "price = float(raw)",
        "kind": "known-violation",
        "reason": "legacy",
        "tracked_by": "H03",
    }
    timer = {
        "file": "src/hft_platform/risk/rule.py",
        "code": "window_s: float = 10.0",
        "kind": "not-money",
        "reason": "window duration in seconds",
    }
    old = yaml.safe_dump({"exemptions": [existing]}).encode()
    supported = yaml.safe_dump({"exemptions": [existing, timer]}).encode()
    expanded = yaml.safe_dump({"exemptions": [{**existing, "count": 2}]}).encode()
    assert list(check_configuration(FLOAT_POLICY, supported, old)) == []
    assert list(check_configuration(FLOAT_POLICY, old, b"")) == [
        f"{FLOAT_POLICY}: HFT-D005 fix the money precision violation in {existing['file']}"
    ]
    assert len(list(check_configuration(FLOAT_POLICY, expanded, old))) == 1


def test_project_config_cannot_move_suppressions_to_mypy_or_coverage():
    config = b"""
[tool.mypy]
exclude = "src"
[[tool.mypy.overrides]]
module = "hft_platform.risk.*"
ignore_errors = true
[tool.coverage.run]
omit = ["src/hft_platform/risk/*"]
"""
    assert list(check_configuration("pyproject.toml", config, config)) == []
    errors = list(check_configuration("pyproject.toml", config, b""))
    assert len(errors) == 3
    assert any("mypy (global): exclude = src" in error for error in errors)
    assert any("mypy (hft_platform.risk.*): ignore_errors" in error for error in errors)
    assert any("coverage.run: omit =" in error for error in errors)
