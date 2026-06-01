"""Round 50: sample_adequacy_label (驗證標準 §4) lifted to a top-level row
field and surfaced in `audit show` + `summary`. A blocking-passed but
sample-insufficient candidate must read as NOT-READY, never complete."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    label: str | None,
    edge: float | None = 12.0,
    strategy_type: str = "taker",
    profile: str = "vm_ul6_strict",
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
    if label is not None:
        advisory.append(
            {
                "name": "min_sample_size",
                "passed": label == "adequate",
                "metrics": {
                    "n_fills": 50.0,
                    "n_days": 20.0,
                    "sample_adequacy_label": label,
                },
                "details": "",
                "error": False,
            }
        )
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name=f"demo_{run_id}",
        instrument="TXFD6",
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


class TestBuildRecordLiftsSampleLabel:
    def test_label_lifted_to_top_level(self, _isolated) -> None:
        _record(run_id="a", label="adequate")
        rows = sub_gate_audit.read_runs()
        assert rows[0]["sample_adequacy_label"] == "adequate"

    def test_absent_when_gate_didnt_run(self, _isolated) -> None:
        _record(run_id="b", label=None)
        rows = sub_gate_audit.read_runs()
        assert "sample_adequacy_label" not in rows[0]

    def test_unrecognized_label_not_lifted(self, _isolated) -> None:
        _record(run_id="c", label="garbage")
        rows = sub_gate_audit.read_runs()
        assert "sample_adequacy_label" not in rows[0]


class TestShowSurfacesSampleLabel:
    def test_adequate_reads_ready(self, _isolated) -> None:
        _record(run_id="s_ok", label="adequate")
        out = audit_cli.show("s_ok")
        assert "sample_adequacy: adequate" in out
        assert "READY" in out.split("sample_adequacy")[1].split("\n")[0]

    def test_promising_reads_not_ready(self, _isolated) -> None:
        _record(run_id="s_pr", label="promising")
        out = audit_cli.show("s_pr")
        line = out.split("sample_adequacy")[1].split("\n")[0]
        assert "promising" in line
        assert "NOT-READY" in line

    def test_needs_more_sample_reads_not_ready(self, _isolated) -> None:
        _record(run_id="s_nm", label="needs_more_sample")
        out = audit_cli.show("s_nm")
        assert "NOT-READY" in out.split("sample_adequacy")[1].split("\n")[0]

    def test_na_when_missing(self, _isolated) -> None:
        _record(run_id="s_na", label=None)
        out = audit_cli.show("s_na")
        assert "sample_adequacy: (n/a" in out


class TestSummaryAggregatesSampleAdequacy:
    def test_summary_counts_labels(self, _isolated) -> None:
        _record(run_id="d_a", label="adequate")
        _record(run_id="d_b", label="promising")
        _record(run_id="d_c", label="needs_more_sample")
        _record(run_id="d_d", label="adequate")
        out = audit_cli.summary()
        assert "sample_adequacy (驗證標準 §4" in out
        section = out.split("sample_adequacy")[1]
        assert "rows with label : 4 / 4" in section
        assert "adequate        : 2" in section
        assert "promising       : 1" in section
        assert "needs_more_sample: 1" in section

    def test_summary_header_renders_without_labels(self, _isolated) -> None:
        _record(run_id="d_none", label=None)
        out = audit_cli.summary()
        section = out.split("sample_adequacy")[1]
        assert "rows with label : 0 / 1" in section
