"""Round 83: `audit template-check` exposes template_field_audit as an operator
command — loads each _SHAPE_TEMPLATES entry, reports §3 field coverage, and
flags drift (a template missing a required field). 完成狀態 §9 固定模板 SOP.
Read-only, audit-layer only."""

from __future__ import annotations

from pathlib import Path

import pytest

from hft_platform.alpha import audit_cli


class TestTemplateCheck:
    def test_shipped_templates_pass(self) -> None:
        # The live templates carry the full §3 field set (guarded in
        # test_strategy_spec too) — the operator command must agree.
        for _shape, (path, _name) in audit_cli._SHAPE_TEMPLATES.items():
            if not Path(path).is_file():
                pytest.skip(f"template not present: {path}")
        out = audit_cli.template_check()
        assert "template field-coverage check" in out
        assert "verdict: all templates cover §3" in out
        assert "DRIFT DETECTED" not in out

    def test_drift_reported_when_template_missing_field(self, monkeypatch, tmp_path) -> None:
        import yaml

        # A template that drops risk_control + cost_model.
        bad = tmp_path / "spec.bad.yaml"
        bad.write_text(
            yaml.safe_dump(
                {
                    "strategy_name": "x",
                    "market": "TAIFEX",
                    "instrument": "TXFD6",
                    "hypothesis": "h",
                    "timeframe": "5m",
                    "holding_period": "intraday",
                    "frequency_class": "intraday_hft",
                    "entry_rule": "e",
                    "exit_rule": "x",
                    "position_sizing": "1 lot",
                    "validation_plan": {},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setitem(
            audit_cli._SHAPE_TEMPLATES, "single", (bad, "demo")
        )
        out = audit_cli.template_check()
        assert "DRIFT DETECTED" in out
        assert "[FAIL]" in out
        assert "risk_control" in out
        assert "cost_model" in out

    def test_missing_template_file_is_flagged(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setitem(
            audit_cli._SHAPE_TEMPLATES,
            "single",
            (tmp_path / "nope.yaml", "demo"),
        )
        out = audit_cli.template_check()
        assert "DRIFT DETECTED" in out
        assert "[MISS]" in out

    def test_cli_dispatch(self, capsys) -> None:
        for _shape, (path, _name) in audit_cli._SHAPE_TEMPLATES.items():
            if not Path(path).is_file():
                pytest.skip(f"template not present: {path}")
        rc = audit_cli.main(["template-check"])
        assert rc == 0
        captured = capsys.readouterr().out
        assert "template field-coverage check" in captured
