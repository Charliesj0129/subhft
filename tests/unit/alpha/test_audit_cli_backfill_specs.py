"""Round 33: ``audit backfill-specs`` covers legacy candidate dirs."""

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


def _mkdir(root: Path, name: str, with_spec: bool = False) -> Path:
    d = root / name
    d.mkdir()
    if with_spec:
        shutil.copyfile(TEMPLATE_SRC, d / "spec.yaml")
    return d


class TestAuditCliBackfillSpecs:
    def test_no_missing_returns_clean_message(self, _alphas: Path) -> None:
        _mkdir(_alphas, "c_one", with_spec=True)
        _mkdir(_alphas, "c_two", with_spec=True)
        out = audit_cli.backfill_specs(template=TEMPLATE_SRC, root=_alphas)
        assert "no missing specs" in out
        assert "2 candidate" in out

    def test_dry_run_lists_targets_without_writing(self, _alphas: Path) -> None:
        _mkdir(_alphas, "legacy_a")
        _mkdir(_alphas, "legacy_b")
        out = audit_cli.backfill_specs(template=TEMPLATE_SRC, root=_alphas)
        assert "DRY-RUN" in out
        assert "legacy_a" in out
        assert "legacy_b" in out
        assert not (_alphas / "legacy_a" / "spec.yaml").exists()
        assert not (_alphas / "legacy_b" / "spec.yaml").exists()

    def test_apply_writes_specs_and_substitutes_name(self, _alphas: Path) -> None:
        _mkdir(_alphas, "legacy_c")
        out = audit_cli.backfill_specs(template=TEMPLATE_SRC, root=_alphas, apply=True)
        assert "APPLY" in out
        target = _alphas / "legacy_c" / "spec.yaml"
        assert target.is_file()
        body = target.read_text()
        assert "strategy_name: legacy_c" in body
        assert "exemplar_txfd6_demo" not in body

    def test_skips_existing_spec(self, _alphas: Path) -> None:
        _mkdir(_alphas, "kept", with_spec=True)
        _mkdir(_alphas, "needs_one")
        out = audit_cli.backfill_specs(template=TEMPLATE_SRC, root=_alphas, apply=True)
        # 'kept' must not appear in the actions list — it had a spec already.
        assert "kept" not in out.split("missing spec.yaml:")[-1]
        assert (_alphas / "needs_one" / "spec.yaml").is_file()

    def test_skips_underscore_prefixed_dirs(self, _alphas: Path) -> None:
        _mkdir(_alphas, "_templates")
        _mkdir(_alphas, "__pycache__")
        _mkdir(_alphas, "real_one")
        out = audit_cli.backfill_specs(template=TEMPLATE_SRC, root=_alphas)
        assert "_templates" not in out
        assert "__pycache__" not in out
        assert "real_one" in out

    def test_refuses_missing_template(self, _alphas: Path, tmp_path: Path) -> None:
        out = audit_cli.backfill_specs(template=tmp_path / "nope.yaml", root=_alphas)
        assert "refused" in out

    def test_refuses_missing_root(self, tmp_path: Path) -> None:
        out = audit_cli.backfill_specs(template=TEMPLATE_SRC, root=tmp_path / "nope")
        assert "refused" in out

    def test_spec_check_pass_count_in_summary(self, _alphas: Path) -> None:
        _mkdir(_alphas, "legacy_d")
        _mkdir(_alphas, "legacy_e")
        out = audit_cli.backfill_specs(template=TEMPLATE_SRC, root=_alphas, apply=True)
        # Fresh template + name substitution leaves a passing spec.
        assert "2 pass / 0 spec_check FAIL" in out

    def test_main_dispatches_backfill_specs(self, _alphas: Path, capsys) -> None:
        _mkdir(_alphas, "from_main")
        rc = audit_cli.main(
            [
                "backfill-specs",
                "--root",
                str(_alphas),
                "--template",
                str(TEMPLATE_SRC),
                "--apply",
            ]
        )
        assert rc == 0
        captured = capsys.readouterr().out
        assert "from_main" in captured
        assert (_alphas / "from_main" / "spec.yaml").is_file()
