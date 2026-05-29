"""Round 32: ``audit init`` scaffolds a candidate spec from the template.

Goal §3 / §9: "固定模板新增策略" should be one command, not a manual
file-copy ritual.  This locks the behavior under test so the template
contract stays stable as it evolves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hft_platform.alpha import audit_cli


TEMPLATE_SRC = Path("research/alphas/_templates/spec.yaml")


@pytest.fixture
def _tmproot(tmp_path: Path) -> Path:
    """Per-test alphas root, isolated from the real research tree."""
    root = tmp_path / "alphas"
    root.mkdir()
    return root


class TestAuditCliInit:
    def test_creates_spec_yaml_in_target_directory(self, _tmproot: Path) -> None:
        out = audit_cli.init_candidate(
            "demo_one", template=TEMPLATE_SRC, root=_tmproot
        )
        target = _tmproot / "demo_one" / "spec.yaml"
        assert target.is_file()
        assert "created" in out
        assert str(target) in out

    def test_default_strategy_name_is_alpha_id(self, _tmproot: Path) -> None:
        audit_cli.init_candidate("demo_two", template=TEMPLATE_SRC, root=_tmproot)
        body = (_tmproot / "demo_two" / "spec.yaml").read_text()
        assert "strategy_name: demo_two" in body
        # The template's literal placeholder must be gone.
        assert "exemplar_txfd6_demo" not in body

    def test_explicit_strategy_name_override(self, _tmproot: Path) -> None:
        audit_cli.init_candidate(
            "demo_three",
            template=TEMPLATE_SRC,
            root=_tmproot,
            strategy_name="custom_name_v1",
        )
        body = (_tmproot / "demo_three" / "spec.yaml").read_text()
        assert "strategy_name: custom_name_v1" in body

    def test_refuses_existing_target_without_force(self, _tmproot: Path) -> None:
        audit_cli.init_candidate("dup", template=TEMPLATE_SRC, root=_tmproot)
        out = audit_cli.init_candidate("dup", template=TEMPLATE_SRC, root=_tmproot)
        assert "refused" in out
        assert "already exists" in out

    def test_force_allows_overwrite(self, _tmproot: Path) -> None:
        audit_cli.init_candidate(
            "ovr",
            template=TEMPLATE_SRC,
            root=_tmproot,
            strategy_name="first",
        )
        out = audit_cli.init_candidate(
            "ovr",
            template=TEMPLATE_SRC,
            root=_tmproot,
            strategy_name="second",
            force=True,
        )
        assert "created" in out
        body = (_tmproot / "ovr" / "spec.yaml").read_text()
        assert "strategy_name: second" in body

    def test_refuses_unsafe_alpha_id(self, _tmproot: Path) -> None:
        assert "refused" in audit_cli.init_candidate("../escape", template=TEMPLATE_SRC, root=_tmproot)
        assert "refused" in audit_cli.init_candidate("a/b", template=TEMPLATE_SRC, root=_tmproot)
        assert "refused" in audit_cli.init_candidate(".hidden", template=TEMPLATE_SRC, root=_tmproot)
        assert "refused" in audit_cli.init_candidate("", template=TEMPLATE_SRC, root=_tmproot)

    def test_refuses_missing_template(self, _tmproot: Path, tmp_path: Path) -> None:
        bogus = tmp_path / "does_not_exist.yaml"
        out = audit_cli.init_candidate("x", template=bogus, root=_tmproot)
        assert "refused" in out

    def test_runs_spec_check_and_reports_pass(self, _tmproot: Path) -> None:
        # The shipped template populates every required field, so the
        # scaffold straight off the template should pass spec_check.
        out = audit_cli.init_candidate(
            "fresh_pass", template=TEMPLATE_SRC, root=_tmproot
        )
        assert "spec_check: PASS" in out

    def test_main_dispatches_init(self, _tmproot: Path, capsys) -> None:
        rc = audit_cli.main(
            [
                "init",
                "from_main",
                "--root",
                str(_tmproot),
                "--template",
                str(TEMPLATE_SRC),
            ]
        )
        assert rc == 0
        captured = capsys.readouterr().out
        assert "created" in captured
        assert (_tmproot / "from_main" / "spec.yaml").is_file()
