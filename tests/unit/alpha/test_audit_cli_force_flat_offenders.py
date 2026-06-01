"""Round 46: `audit force-flat` enumerates runs whose edge is propped up
by force-flat marks (force_flat_trip_share_pct over the strict cap)."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    ff_share: float | None,
    edge: float | None = 12.0,
    strategy_type: str = "taker",
    profile: str = "vm_ul6_strict",
    instrument: str = "TXFD6",
) -> None:
    advisory: list[dict] = []
    if edge is not None:
        advisory.append(
            {
                "name": "edge_per_round_trip",
                "passed": edge > 10.0,
                "metrics": {"mean_net_edge_pts_per_trade": edge},
                "details": "",
                "error": False,
            }
        )
    if ff_share is not None:
        advisory.append(
            {
                "name": "force_flat_residual",
                "passed": ff_share <= 30.0,
                "metrics": {"force_flat_trip_share_pct": ff_share},
                "details": "",
                "error": False,
            }
        )
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name=f"demo_{run_id}",
        instrument=instrument,
        strategy_type=strategy_type,
        profile_name=profile,
        advisory=advisory,
        blocking={"passed": True, "failing": [], "triage_status": "passed"},
        recorded_at_ns=1,
    )


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    sub_gate_audit._reset_cache_for_tests()
    return path


class TestForceFlatOffenders:
    def test_lists_only_rows_over_cap(self, _isolated) -> None:
        _record(run_id="over_a", ff_share=45.0)
        _record(run_id="under_b", ff_share=10.0)
        _record(run_id="over_c", ff_share=80.0)
        out = audit_cli.force_flat_offenders()
        assert "over_a" in out
        assert "over_c" in out
        assert "under_b" not in out

    def test_boundary_value_at_cap_is_excluded(self, _isolated) -> None:
        # strictly-exceeds: exactly 30.0% is within the cap, not an offender.
        _record(run_id="exactly_cap", ff_share=30.0)
        out = audit_cli.force_flat_offenders()
        assert "exactly_cap" not in out
        assert "no rows over force_flat cap" in out

    def test_sorted_by_share_descending(self, _isolated) -> None:
        _record(run_id="mid", ff_share=50.0)
        _record(run_id="worst", ff_share=90.0)
        _record(run_id="least", ff_share=35.0)
        out = audit_cli.force_flat_offenders()
        assert out.index("worst") < out.index("mid") < out.index("least")

    def test_rows_without_metric_are_skipped(self, _isolated) -> None:
        _record(run_id="no_metric", ff_share=None)
        _record(run_id="has_metric", ff_share=55.0)
        out = audit_cli.force_flat_offenders()
        assert "no_metric" not in out
        assert "has_metric" in out

    def test_custom_min_share_threshold(self, _isolated) -> None:
        _record(run_id="r20", ff_share=20.0)
        _record(run_id="r40", ff_share=40.0)
        out = audit_cli.force_flat_offenders(min_share=15.0)
        assert "r20" in out
        assert "r40" in out

    def test_strategy_type_filter(self, _isolated) -> None:
        _record(run_id="taker_off", ff_share=60.0, strategy_type="taker")
        _record(run_id="maker_off", ff_share=70.0, strategy_type="maker")
        out = audit_cli.force_flat_offenders(strategy_type="maker")
        assert "maker_off" in out
        assert "taker_off" not in out

    def test_edge_surfaced_in_row(self, _isolated) -> None:
        _record(run_id="with_edge", ff_share=44.0, edge=12.345)
        out = audit_cli.force_flat_offenders()
        assert "12.345" in out

    def test_empty_audit_reports_no_match(self, _isolated) -> None:
        out = audit_cli.force_flat_offenders()
        assert "no audit rows match filter." in out


class TestForceFlatCli:
    def test_main_force_flat_subcommand_returns_zero(self, _isolated, capsys) -> None:
        _record(run_id="cli_over", ff_share=66.0)
        rc = audit_cli.main(["force-flat"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "cli_over" in captured.out

    def test_main_force_flat_min_share_flag(self, _isolated, capsys) -> None:
        _record(run_id="cli_25", ff_share=25.0)
        rc = audit_cli.main(["force-flat", "--min-share", "20"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "cli_25" in captured.out
