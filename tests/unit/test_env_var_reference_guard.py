from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "env_var_reference_guard.py"
    spec = importlib.util.spec_from_file_location("env_var_reference_guard", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_extract_hft_vars_deduplicates():
    mod = _load_module()
    text = "HFT_A=1 HFT_A=1 HFT_B"
    assert mod._extract_hft_vars(text) == {"HFT_A", "HFT_B"}


def test_guard_fails_when_runbook_var_missing_in_reference(tmp_path: Path):
    mod = _load_module()
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "docs").mkdir()
    (project_root / "docs" / "runbooks.md").write_text("Use `HFT_ALPHA` and `HFT_BETA`.\n", encoding="utf-8")
    (project_root / "docs" / "operations").mkdir()
    (project_root / "docs" / "operations" / "env-vars-reference.md").write_text(
        "# refs\n\n- `HFT_ALPHA`\n- [runbook](../runbooks.md)\n",
        encoding="utf-8",
    )

    payload = mod._evaluate_reference_guard(
        project_root=project_root,
        reference_doc=project_root / "docs" / "operations" / "env-vars-reference.md",
        runbook_files=[project_root / "docs" / "runbooks.md"],
    )
    assert payload["overall"] == mod.STATUS_FAIL
    assert payload["missing_vars"] == ["HFT_BETA"]


def test_main_passes_and_writes_artifact(tmp_path: Path):
    mod = _load_module()
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / "docs").mkdir()
    (project_root / "docs" / "runbooks").mkdir()
    (project_root / "docs" / "operations").mkdir(parents=True, exist_ok=True)

    (project_root / "docs" / "runbooks.md").write_text("`HFT_ALPHA`\n", encoding="utf-8")
    (project_root / "docs" / "runbooks" / "x.md").write_text("`HFT_BETA`\n", encoding="utf-8")
    (project_root / "docs" / "operations" / "env-vars-reference.md").write_text(
        "# refs\n\n- `HFT_ALPHA`\n- `HFT_BETA`\n- [main runbook](../runbooks.md)\n",
        encoding="utf-8",
    )

    rc = mod.main(
        [
            "--project-root",
            str(project_root),
            "--output-dir",
            "outputs/env_var_guard",
        ]
    )
    assert rc == 0

    reports = list((project_root / "outputs" / "env_var_guard" / "checks").glob("env_vars_guard_*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["overall"] == mod.STATUS_PASS


# ── both directions: code <-> reference (report only) ──


def _src_repo(tmp_path: Path, source: str) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "hft_platform").mkdir(parents=True)
    (root / "src" / "hft_platform" / "m.py").write_text(source, encoding="utf-8")
    return root


def test_scan_code_vars_sees_helper_reads_literal_defaults_and_skips_prose(tmp_path: Path):
    mod = _load_module()
    root = _src_repo(
        tmp_path,
        '"""Set HFT_IN_DOCSTRING to tune."""\n'
        'a = os.getenv("HFT_PLAIN", "5")\n'
        'b = _env_int("HFT_HELPER", 7)\n'
        'c = flag("HFT_KW", default=True)\n'
        'd = os.environ.get("HFT_DYNAMIC", compute())\n'
        'e = os.environ["HFT_SUBSCRIPT"]\n',
    )

    found = mod.scan_code_vars(root)

    assert set(found) == {"HFT_PLAIN", "HFT_HELPER", "HFT_KW", "HFT_DYNAMIC", "HFT_SUBSCRIPT"}
    assert found["HFT_PLAIN"]["defaults"] == {"5"}
    assert found["HFT_HELPER"]["defaults"] == {"7"}
    assert found["HFT_KW"]["defaults"] == {"1"}
    assert found["HFT_DYNAMIC"]["defaults"] == set()
    assert found["HFT_PLAIN"]["sites"] == ["src/hft_platform/m.py:2"]


def test_defaults_compare_by_value_not_spelling():
    mod = _load_module()

    assert mod._normalize_default("1") == mod._normalize_default(True) == mod._normalize_default(1.0) == "1"
    assert mod._normalize_default("true") == mod._normalize_default("YES") == "1"
    assert mod._normalize_default("0.50") == mod._normalize_default(0.5) == "0.5"
    assert mod._normalize_default("") is None and mod._normalize_default(None) is None
    assert mod._normalize_default(" data/wal ") == "data/wal"


def test_reference_defaults_skip_prose_unset_and_lists():
    mod = _load_module()
    text = (
        "| `HFT_A` | `5` | x |\n"
        "| `HFT_B` | — | x |\n"
        "| `HFT_C` | `a, b` | x |\n"
        "| `HFT_D` | 見說明 | x |\n"
        "| `HFT_E` | `true` | x |\n"
        "| `HFT_A` | `9` | duplicate row keeps the first |\n"
    )

    assert mod.parse_reference_defaults(text) == {
        "HFT_A": "5",
        "HFT_B": None,
        "HFT_C": None,
        "HFT_D": None,
        "HFT_E": "1",
    }


def test_directions_report_each_kind_of_drift(tmp_path: Path):
    mod = _load_module()
    reference = (
        "| `HFT_DOCUMENTED` | `1` | ok |\n"
        "| `HFT_WRONG` | `10` | x |\n"
        "| `HFT_GONE` | `1` | no reader anywhere |\n"
        "| `HFT_COMPOSE_ONLY` | `1` | wired in compose |\n"
        "| `HFT_PREFIX_` | — | a family name |\n"
    )
    code = {
        "HFT_DOCUMENTED": {"sites": ["a.py:1"], "defaults": {"1"}},
        "HFT_WRONG": {"sites": ["a.py:2"], "defaults": {"30"}},
        "HFT_NEW": {"sites": ["a.py:3", "b.py:4"], "defaults": set()},
        "HFT_SPLIT": {"sites": ["a.py:5"], "defaults": {"0", "1"}},
        "HFT_FAMILY_": {"sites": ["a.py:6"], "defaults": set()},  # a prefix scanned for, not a variable
    }

    out = mod.evaluate_directions(tmp_path, reference, code, {"HFT_COMPOSE_ONLY"})

    assert [r["var"] for r in out["undocumented_code_vars"]] == ["HFT_NEW", "HFT_SPLIT"]
    assert out["undocumented_code_vars"][0]["sites"] == ["a.py:3", "b.py:4"]
    assert out["unread_reference_vars"] == ["HFT_GONE"]
    assert out["default_mismatches"] == [{"var": "HFT_WRONG", "documented": "10", "code": ["30"]}]
    assert out["multiple_code_defaults"] == [{"var": "HFT_SPLIT", "defaults": ["0", "1"]}]


def test_other_readers_find_names_in_compose_scripts_and_rust(tmp_path: Path):
    mod = _load_module()
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "rust_core" / "src").mkdir(parents=True)
    (root / "scripts" / "run.sh").write_text("export HFT_FROM_SCRIPT=1\n", encoding="utf-8")
    (root / "rust_core" / "src" / "lib.rs").write_text('std::env::var("HFT_FROM_RUST");\n', encoding="utf-8")
    (root / "docker-compose.yml").write_text(
        "environment:\n  - HFT_FROM_COMPOSE=${HFT_FROM_COMPOSE:-1}\n", encoding="utf-8"
    )

    assert mod.scan_other_readers(root) == {"HFT_FROM_SCRIPT", "HFT_FROM_RUST", "HFT_FROM_COMPOSE"}


def _drifting_repo(tmp_path: Path) -> Path:
    root = _src_repo(tmp_path, 'x = os.getenv("HFT_UNDOCUMENTED", "1")\n')
    (root / "docs" / "runbooks").mkdir(parents=True)
    (root / "docs" / "operations").mkdir(parents=True)
    (root / "docs" / "runbooks.md").write_text("`HFT_ALPHA`\n", encoding="utf-8")
    (root / "docs" / "operations" / "env-vars-reference.md").write_text(
        "| `HFT_ALPHA` | `1` | x |\n- [main runbook](../runbooks.md)\n", encoding="utf-8"
    )
    return root


def test_direction_findings_do_not_change_the_exit_code_unless_strict(tmp_path: Path):
    mod = _load_module()
    root = _drifting_repo(tmp_path)
    args = ["--project-root", str(root), "--output-dir", "outputs/env_var_guard"]

    assert mod.main(args) == 0
    assert mod.main([*args, "--strict-directions"]) == 2

    report = next((root / "outputs" / "env_var_guard" / "checks").glob("env_vars_guard_*.json"))
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert [r["var"] for r in payload["directions"]["undocumented_code_vars"]] == ["HFT_UNDOCUMENTED"]
    assert payload["directions"]["unread_reference_vars"] == ["HFT_ALPHA"]
