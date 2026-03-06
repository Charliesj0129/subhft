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
