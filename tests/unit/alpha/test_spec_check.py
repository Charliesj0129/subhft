"""Tests for the strategy_spec CI gate (Round 12 / goal §3)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hft_platform.alpha import spec_check


def _valid_spec_dict() -> dict:
    return {
        "strategy_name": "c99_demo",
        "market": "TAIFEX",
        "instrument": "TXFD6",
        "hypothesis": "h",
        "timeframe": "5m",
        "holding_period": "intraday",
        "frequency_class": "intraday_hft",
        "entry_rule": "e",
        "exit_rule": "x",
        "position_sizing": "fixed 1 lot",
        "risk_control": {
            "max_position": 1,
            "max_drawdown_pts": 80,
            "force_flat_rule": "13:25 close",
        },
        "cost_model": {
            "fee_bps": 0.4,
            "tax_bps": 2.0,
            "slippage_pts": 0.5,
            "latency_profile": "shioaji_measured_p95",
        },
        "validation_plan": {
            "data_range": "2026-01-02..2026-05-13",
            "oos_split": "70/30 by trading day",
            "sample_targets": {
                "min_round_trips": 300,
                "min_oos_trading_days": 60,
            },
            "required_gates": ["min_sample_size"],
            "net_edge_floor_pts": 10.0,
        },
    }


def _write(path: Path, spec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(spec), encoding="utf-8")


class TestCheckOne:
    def test_valid_spec_passes(self, tmp_path: Path) -> None:
        p = tmp_path / "spec.yaml"
        _write(p, _valid_spec_dict())
        passed, errors = spec_check.check_one(p)
        assert passed is True
        assert errors == []

    def test_invalid_spec_reports_errors(self, tmp_path: Path) -> None:
        spec = _valid_spec_dict()
        spec.pop("hypothesis")
        spec["validation_plan"]["net_edge_floor_pts"] = 5.0
        p = tmp_path / "spec.yaml"
        _write(p, spec)
        passed, errors = spec_check.check_one(p)
        assert passed is False
        assert any("hypothesis" in e for e in errors)
        assert any("net_edge_floor_pts" in e for e in errors)

    def test_missing_file_reported(self, tmp_path: Path) -> None:
        passed, errors = spec_check.check_one(tmp_path / "no_such.yaml")
        assert passed is False
        assert any("not found" in e for e in errors)

    def test_non_mapping_yaml_reported(self, tmp_path: Path) -> None:
        p = tmp_path / "spec.yaml"
        p.write_text("- one\n- two\n", encoding="utf-8")
        passed, errors = spec_check.check_one(p)
        assert passed is False
        assert any("mapping" in e for e in errors)


class TestDiscoverSpecs:
    def test_finds_candidate_specs_under_root(self, tmp_path: Path) -> None:
        for name in ("c01", "c02"):
            _write(tmp_path / name / "spec.yaml", _valid_spec_dict())
        # candidate without a spec — should be skipped silently
        (tmp_path / "c03").mkdir()
        # non-directory entry — should be ignored
        (tmp_path / "stray.txt").write_text("noise", encoding="utf-8")
        found = spec_check.discover_specs(tmp_path)
        assert [p.parent.name for p in found] == ["c01", "c02"]

    def test_missing_root_returns_empty(self, tmp_path: Path) -> None:
        assert spec_check.discover_specs(tmp_path / "nope") == []

    def test_order_is_sorted(self, tmp_path: Path) -> None:
        for name in ("z_last", "a_first", "m_mid"):
            _write(tmp_path / name / "spec.yaml", _valid_spec_dict())
        found = spec_check.discover_specs(tmp_path)
        assert [p.parent.name for p in found] == ["a_first", "m_mid", "z_last"]


class TestMain:
    def test_single_valid_path_exit_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        p = tmp_path / "spec.yaml"
        _write(p, _valid_spec_dict())
        rc = spec_check.main([str(p)])
        assert rc == 0
        assert "[ok]" in capsys.readouterr().out

    def test_single_invalid_path_exit_one(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        spec = _valid_spec_dict()
        spec.pop("entry_rule")
        p = tmp_path / "spec.yaml"
        _write(p, spec)
        rc = spec_check.main([str(p)])
        assert rc == 1
        out = capsys.readouterr().out
        assert "[FAIL]" in out
        assert "entry_rule" in out

    def test_all_flag_scans_root(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write(tmp_path / "c01" / "spec.yaml", _valid_spec_dict())
        bad = _valid_spec_dict()
        bad.pop("hypothesis")
        _write(tmp_path / "c02" / "spec.yaml", bad)
        rc = spec_check.main(["--all", "--root", str(tmp_path)])
        assert rc == 1
        out = capsys.readouterr().out
        assert "c01" in out
        assert "c02" in out
        # The good one prints [ok], the bad one prints [FAIL] + the
        # specific missing field — one pass surfaces every gap.
        assert "[ok]" in out
        assert "[FAIL]" in out
        assert "hypothesis" in out

    def test_all_flag_all_valid_exit_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write(tmp_path / "c01" / "spec.yaml", _valid_spec_dict())
        _write(tmp_path / "c02" / "spec.yaml", _valid_spec_dict())
        rc = spec_check.main(["--all", "--root", str(tmp_path)])
        assert rc == 0

    def test_all_flag_empty_root_exits_one(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        # Silent zero would mask a wiring break (e.g. wrong --root).
        rc = spec_check.main(["--all", "--root", str(tmp_path)])
        assert rc == 1
        assert "no spec.yaml" in capsys.readouterr().out

    def test_path_and_all_are_mutually_exclusive(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            spec_check.main([str(tmp_path / "spec.yaml"), "--all"])


class TestExemplarSpec:
    """The shipped exemplar at research/alphas/_templates/spec.yaml is
    SOP's reference implementation; if it ever drifts out of compliance,
    every author copying it inherits the breakage.  Lock it down."""

    def test_repo_exemplar_passes_gate(self) -> None:
        exemplar = Path("research/alphas/_templates/spec.yaml")
        assert exemplar.is_file(), "exemplar spec missing from _templates/"
        passed, errors = spec_check.check_one(exemplar)
        assert passed is True, errors
