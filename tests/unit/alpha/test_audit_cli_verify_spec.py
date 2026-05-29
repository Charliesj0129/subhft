"""Round 34: ``audit verify-spec`` per-field fill-state for candidate specs."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hft_platform.alpha import audit_cli


TEMPLATE_SRC = Path("research/alphas/_templates/spec.yaml")


@pytest.fixture
def _alphas(tmp_path: Path) -> Path:
    root = tmp_path / "alphas"
    root.mkdir()
    return root


def _seed(root: Path, name: str, *, with_spec: bool = True) -> Path:
    d = root / name
    d.mkdir()
    if with_spec:
        shutil.copyfile(TEMPLATE_SRC, d / "spec.yaml")
    return d


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


class TestFieldStateHelper:
    def test_missing_for_none_empty(self) -> None:
        assert audit_cli._field_state(None) == "missing"
        assert audit_cli._field_state("") == "missing"
        assert audit_cli._field_state("   ") == "missing"
        assert audit_cli._field_state([]) == "missing"
        assert audit_cli._field_state({}) == "missing"

    def test_placeholder_detects_todo_and_template_marker(self) -> None:
        assert audit_cli._field_state("TODO: write hypothesis") == "placeholder"
        assert audit_cli._field_state("exemplar_txfd6_demo") == "placeholder"
        assert audit_cli._field_state(["x", "FILLME y"]) == "placeholder"
        assert audit_cli._field_state({"k": "PLACEHOLDER"}) == "placeholder"

    def test_set_for_concrete_value(self) -> None:
        assert audit_cli._field_state("alpha_one") == "set"
        assert audit_cli._field_state(["a", "b"]) == "set"
        assert audit_cli._field_state({"k": 1}) == "set"
        assert audit_cli._field_state(42) == "set"


class TestVerifySpecSingle:
    def test_refuses_missing_root(self, tmp_path: Path) -> None:
        out = audit_cli.verify_spec("x", root=tmp_path / "missing")
        assert "refused" in out

    def test_refuses_when_no_alpha_id_and_no_all(self, _alphas: Path) -> None:
        out = audit_cli.verify_spec(None, root=_alphas)
        assert "refused" in out

    def test_no_spec_file_reports_clearly(self, _alphas: Path) -> None:
        _seed(_alphas, "barebones", with_spec=False)
        out = audit_cli.verify_spec("barebones", root=_alphas)
        assert "no spec.yaml" in out

    def test_fresh_template_reports_all_set_and_pass(self, _alphas: Path) -> None:
        _seed(_alphas, "fresh")
        # Substitute the placeholder strategy_name to a real value first
        # so it doesn't trip the placeholder detector.
        path = _alphas / "fresh" / "spec.yaml"
        body = path.read_text().replace("exemplar_txfd6_demo", "fresh")
        _write(path, body)
        out = audit_cli.verify_spec("fresh", root=_alphas)
        assert "[OK ] strategy_name" in out
        assert "placeholder=0" in out
        assert "missing=0" in out
        assert "spec_check: PASS" in out

    def test_detects_placeholder_in_template_strategy_name(self, _alphas: Path) -> None:
        # Unmodified template -> strategy_name == exemplar_txfd6_demo -> placeholder.
        _seed(_alphas, "raw_template")
        out = audit_cli.verify_spec("raw_template", root=_alphas)
        assert "[TODO] strategy_name" in out


class TestVerifySpecAll:
    def test_all_returns_table_across_dirs(self, _alphas: Path) -> None:
        _seed(_alphas, "alpha_a")
        _seed(_alphas, "alpha_b", with_spec=False)
        out = audit_cli.verify_spec(None, root=_alphas, all_specs=True)
        assert "alpha_a" in out
        assert "alpha_b" in out
        assert "NO_SPEC" in out
        assert "(2 candidates scanned)" in out

    def test_all_skips_underscore_directories(self, _alphas: Path) -> None:
        _seed(_alphas, "_templates")
        _seed(_alphas, "__pycache__", with_spec=False)
        _seed(_alphas, "real")
        out = audit_cli.verify_spec(None, root=_alphas, all_specs=True)
        assert "_templates" not in out
        assert "__pycache__" not in out
        assert "real" in out

    def test_all_with_no_real_dirs_message(self, _alphas: Path) -> None:
        _seed(_alphas, "_templates")
        out = audit_cli.verify_spec(None, root=_alphas, all_specs=True)
        assert "no candidate directories" in out

    def test_parse_error_recorded(self, _alphas: Path) -> None:
        _seed(_alphas, "broken", with_spec=False)
        _write(_alphas / "broken" / "spec.yaml", "this: is: not: yaml: ::")
        out = audit_cli.verify_spec(None, root=_alphas, all_specs=True)
        assert "PARSE_ERR" in out

    def test_main_dispatches_verify_spec(self, _alphas: Path, capsys) -> None:
        _seed(_alphas, "from_main")
        rc = audit_cli.main(["verify-spec", "from_main", "--root", str(_alphas)])
        assert rc == 0
        captured = capsys.readouterr().out
        assert "from_main" in captured
        assert "summary" in captured
