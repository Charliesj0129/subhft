"""Tests for the sub-gate audit query CLI (Round 10 / goal §9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _advisory(*, edge: float, label: str = "promising") -> list[dict]:
    return [
        {
            "name": "min_sample_size",
            "passed": False,
            "metrics": {
                "n_fills": 240.0,
                "n_days": 50.0,
                "sample_adequacy_label": label,
            },
            "details": f"fills=240 (min 300), label={label}",
        },
        {
            "name": "edge_per_round_trip",
            "passed": edge > 10.0,
            "metrics": {"mean_net_edge_pts_per_trade": edge},
            "details": f"edge={edge} vs min 10.0",
        },
    ]


def _blocking(status: str, reasons: list[str]) -> dict:
    return {
        "passed": status == "passed",
        "failing": [{"name": r, "passed": False, "metrics": {}, "details": ""} for r in reasons],
        "names": reasons,
        "profile": "vm_ul6_strict",
        "triage_status": status,
        "triage_reasons": reasons,
    }


@pytest.fixture
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "sub_gate_runs.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    sub_gate_audit._reset_cache_for_tests()
    return path


def _write(
    *,
    run_id: str,
    strategy_type: str = "maker",
    edge: float = 8.0,
    status: str = "sample_promising",
    reasons: list[str] | None = None,
    strategy_name: str = "r47",
    label: str = "promising",
) -> None:
    if reasons is None:
        reasons = ["min_sample_size"]
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name=strategy_name,
        instrument="TMFD6",
        strategy_type=strategy_type,
        profile_name="vm_ul6_strict",
        advisory=_advisory(edge=edge, label=label),
        blocking=_blocking(status, reasons),
    )


class TestShow:
    def test_returns_not_found_message_for_unknown_run_id(self, _isolated: Path) -> None:
        out = audit_cli.show("missing")
        assert out.startswith("no audit row")

    def test_renders_known_run(self, _isolated: Path) -> None:
        _write(run_id="r1", edge=8.0, status="sample_promising")
        out = audit_cli.show("r1")
        assert "run_id          : r1" in out
        assert "triage_status   : sample_promising" in out
        assert "triage_reasons  : min_sample_size" in out
        assert "[FAIL] min_sample_size" in out
        assert "[FAIL] edge_per_round_trip" in out  # 8 < 10

    def test_strategy_type_filter(self, _isolated: Path) -> None:
        _write(run_id="r1", strategy_type="maker", strategy_name="m_strat")
        _write(run_id="r1", strategy_type="taker", strategy_name="t_strat")
        out_maker = audit_cli.show("r1", strategy_type="maker")
        out_taker = audit_cli.show("r1", strategy_type="taker")
        assert "m_strat" in out_maker
        assert "t_strat" in out_taker

    def test_strategy_type_filter_misses_returns_not_found(
        self, _isolated: Path
    ) -> None:
        _write(run_id="r1", strategy_type="maker")
        out = audit_cli.show("r1", strategy_type="taker")
        assert out.startswith("no audit row")
        assert "taker" in out

    def test_default_prefers_maker_when_both_present(self, _isolated: Path) -> None:
        # Insert taker first to verify the picker isn't just "first row".
        _write(run_id="r1", strategy_type="taker", strategy_name="t_strat")
        _write(run_id="r1", strategy_type="maker", strategy_name="m_strat")
        out = audit_cli.show("r1")
        assert "m_strat" in out


class TestCompare:
    def test_reports_missing_rows(self, _isolated: Path) -> None:
        _write(run_id="r1")
        out = audit_cli.compare("r1", "r_missing")
        assert "missing audit row(s)" in out
        assert "r_missing" in out

    def test_triage_transition_and_metric_drift(self, _isolated: Path) -> None:
        _write(run_id="r1", edge=8.0, status="sample_promising")
        _write(
            run_id="r2",
            edge=12.5,
            status="killed",
            reasons=["single_day_dominance"],
        )
        out = audit_cli.compare("r1", "r2")
        assert "triage_status : 'sample_promising' -> 'killed'" in out
        assert "edge_per_round_trip" in out
        assert "mean_net_edge_pts_per_trade: 8.0 -> 12.5" in out

    def test_passed_flip_recorded(self, _isolated: Path) -> None:
        _write(run_id="r1", edge=8.0)
        _write(run_id="r2", edge=12.5)
        out = audit_cli.compare("r1", "r2")
        # edge_per_round_trip flips False -> True
        assert "edge_per_round_trip: passed False -> True" in out

    def test_gate_only_in_one_side_reported(self, _isolated: Path) -> None:
        # Manually craft an A-side missing one gate to exercise the
        # "+ only in B" branch.  We bypass _write so the schemas differ.
        sub_gate_audit.record_sub_gate_run(
            run_id="r_a",
            strategy_name="x",
            instrument="TMFD6",
            strategy_type="maker",
            profile_name="vm_ul6_strict",
            advisory=[
                {
                    "name": "edge_per_round_trip",
                    "passed": True,
                    "metrics": {"mean_net_edge_pts_per_trade": 12.0},
                    "details": "",
                }
            ],
            blocking=_blocking("passed", []),
        )
        sub_gate_audit.record_sub_gate_run(
            run_id="r_b",
            strategy_name="x",
            instrument="TMFD6",
            strategy_type="maker",
            profile_name="vm_ul6_strict",
            advisory=_advisory(edge=12.0, label="adequate"),
            blocking=_blocking("passed", []),
        )
        out = audit_cli.compare("r_a", "r_b")
        assert "+ min_sample_size (only in B)" in out


class TestMain:
    def test_show_command_prints_and_exit_zero(
        self, _isolated: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write(run_id="r1")
        rc = audit_cli.main(["show", "r1"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "run_id          : r1" in out

    def test_show_unknown_exits_one(
        self, _isolated: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = audit_cli.main(["show", "missing"])
        assert rc == 1
        assert "no audit row" in capsys.readouterr().out

    def test_compare_command_works(
        self, _isolated: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write(run_id="r1", edge=8.0)
        _write(run_id="r2", edge=12.5)
        rc = audit_cli.main(["compare", "r1", "r2"])
        assert rc == 0
        assert "triage_status" in capsys.readouterr().out

    def test_compare_missing_exits_one(
        self, _isolated: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write(run_id="r1")
        rc = audit_cli.main(["compare", "r1", "r_missing"])
        assert rc == 1

    def test_strategy_type_arg_propagates(
        self, _isolated: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write(run_id="r1", strategy_type="taker", strategy_name="t_only")
        rc = audit_cli.main(["show", "r1", "--strategy-type", "taker"])
        assert rc == 0
        assert "t_only" in capsys.readouterr().out
