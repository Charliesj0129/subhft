"""Round 31: ``audit gates`` per-sub-gate failure-frequency view."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    strategy_type: str = "maker",
    profile: str = "vm_ul6_strict",
    sub_gates: list[dict],
) -> None:
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name="demo",
        instrument="TXFD6",
        strategy_type=strategy_type,
        profile_name=profile,
        advisory=sub_gates,
        blocking={"passed": True, "failing": [], "triage_status": "passed"},
        recorded_at_ns=1,
    )


def _g(name: str, passed: bool | None, error: bool = False) -> dict:
    return {"name": name, "passed": passed, "metrics": {}, "details": "", "error": error}


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    sub_gate_audit._reset_cache_for_tests()
    return path


class TestAuditCliGates:
    def test_empty_audit_returns_no_match_message(self, _isolated) -> None:
        assert "no audit rows match" in audit_cli.gates()

    def test_rows_with_no_sub_gates_return_specific_message(self, _isolated) -> None:
        _record(run_id="g_empty", sub_gates=[])
        out = audit_cli.gates()
        assert "no sub-gate entries" in out

    def test_aggregates_evaluated_failed_errored_per_gate(self, _isolated) -> None:
        _record(
            run_id="g_a",
            sub_gates=[
                _g("min_sample_size", False),
                _g("sharpe_threshold", True),
                _g("replay_parity", None, error=True),
            ],
        )
        _record(
            run_id="g_b",
            sub_gates=[
                _g("min_sample_size", False),
                _g("sharpe_threshold", False),
            ],
        )
        out = audit_cli.gates()
        # min_sample_size: 2 evaluated, 2 failed → top of the table.
        lines = out.split("\n")
        # Header + separator + body lines + footer.
        body = [ln for ln in lines if ln and not ln.startswith(("sub_gate", "---", "("))]
        assert body[0].startswith("min_sample_size")
        assert "100.0%" in body[0]
        st = next(ln for ln in body if ln.startswith("sharpe_threshold"))
        assert "50.0%" in st
        rp = next(ln for ln in body if ln.startswith("replay_parity"))
        # 1 evaluated, 0 failed, 1 errored, 0.0% fail rate.
        assert "0.0%" in rp
        assert rp.split()[1:5] == ["1", "0", "1", "0.0%"]

    def test_sorted_by_failed_descending(self, _isolated) -> None:
        _record(
            run_id="g_sort",
            sub_gates=[
                _g("low_fail", False),
                _g("low_fail", True),  # 1 fail / 2 evaluated
                _g("hi_fail", False),
                _g("hi_fail", False),
                _g("hi_fail", False),  # 3 fails
            ],
        )
        out = audit_cli.gates()
        i_hi = out.index("hi_fail")
        i_lo = out.index("low_fail")
        assert i_hi < i_lo

    def test_top_truncates(self, _isolated) -> None:
        _record(
            run_id="g_top",
            sub_gates=[
                _g("a_gate", False),
                _g("b_gate", False),
                _g("c_gate", False),
            ],
        )
        out = audit_cli.gates(top=2)
        assert "(2 sub-gates across 1 rows)" in out
        # 3 entries -> top=2 keeps 2 of them.  The 'sub_gate' header also
        # contains '_gate'; subtract its 1 occurrence.
        body_count = out.count("_gate") - 1  # one for the header
        assert body_count == 2

    def test_filter_strategy_type(self, _isolated) -> None:
        _record(run_id="g_m", strategy_type="maker", sub_gates=[_g("maker_only", False)])
        _record(run_id="g_t", strategy_type="taker", sub_gates=[_g("taker_only", False)])
        out = audit_cli.gates(strategy_type="maker")
        assert "maker_only" in out
        assert "taker_only" not in out

    def test_filter_profile(self, _isolated) -> None:
        _record(run_id="g_str", profile="vm_ul6_strict", sub_gates=[_g("strict_x", False)])
        _record(run_id="g_loose", profile="loose", sub_gates=[_g("loose_x", False)])
        out = audit_cli.gates(profile="vm_ul6_strict")
        assert "strict_x" in out
        assert "loose_x" not in out

    def test_footer_reports_row_count(self, _isolated) -> None:
        _record(run_id="g_r1", sub_gates=[_g("gate_x", True)])
        _record(run_id="g_r2", sub_gates=[_g("gate_x", False)])
        out = audit_cli.gates()
        assert "across 2 rows" in out

    def test_main_dispatches_gates(self, _isolated, capsys) -> None:
        _record(run_id="g_main", sub_gates=[_g("g_main_only", False)])
        rc = audit_cli.main(["gates", "--top", "5"])
        assert rc == 0
        captured = capsys.readouterr().out
        assert "g_main_only" in captured
