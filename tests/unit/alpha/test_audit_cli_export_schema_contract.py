"""Round 85: lock the 完成狀態 §9 (輸出統一 metrics) export schema contract.

Two invariants a future change could silently break:
  1. `_export_row` emits exactly the keys declared in `_EXPORT_COLUMNS`
     (no orphan column, no undeclared key) — else csv.DictWriter would raise
     or drop data at runtime.
  2. Every promotion-axis field surfaced in `_SCORECARD_AXES` has a
     corresponding export column — so a new credibility axis can't be added
     to the scorecard yet silently dropped from the unified-metrics export.

Test-only; no source change. Audit-layer scope."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(*, run_id: str, full: bool) -> None:
    advisory: list[dict] = [
        {
            "name": "edge_per_round_trip",
            "passed": True,
            "metrics": {"mean_net_edge_pts_per_trade": 12.0},
            "details": "",
        }
    ]
    if full:
        advisory += [
            {
                "name": "force_flat_residual",
                "passed": True,
                "metrics": {"force_flat_trip_share_pct": 10.0},
                "details": "",
            },
            {
                "name": "single_day_dominance",
                "passed": True,
                "metrics": {"top_day_contribution_pct": 15.0},
                "details": "",
            },
            {
                "name": "min_sample_size",
                "passed": True,
                "metrics": {"sample_adequacy_label": "adequate"},
                "details": "",
            },
            {
                "name": "monthly_distribution",
                "passed": True,
                "metrics": {
                    "drawdown_to_avg_monthly_ratio": 1.5,
                    "top_month_contribution_pct": 40.0,
                    "median_monthly_net_pnl_pts": 30.0,
                    "worst_monthly_pnl_pts": 5.0,
                },
                "details": "",
            },
            {
                "name": "trade_concentration",
                "passed": True,
                "metrics": {"n_trades": 80.0, "worst_loss_share_pct": 30.0},
                "details": "",
            },
            {
                "name": "replay_parity",
                "passed": True,
                "metrics": {"match_pct": 99.0, "threshold": 95.0},
                "details": "",
            },
        ]
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name=f"demo_{run_id}",
        instrument="TXFD6",
        strategy_type="taker",
        profile_name="vm_ul6_strict",
        advisory=advisory,
        blocking={"passed": True, "failing": [], "triage_status": "passed"},
        recorded_at_ns=1,
        spec_provenance={
            "data_range": "2026-Q1",
            "cost_model_id": "measured+1bp/2bp/1pts",
            "required_gates": ["edge_per_round_trip"],
        }
        if full
        else None,
    )


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    sub_gate_audit._reset_cache_for_tests()
    return path


def _row(run_id: str) -> dict:
    return sub_gate_audit.read_runs(run_id)[0]


class TestExportSchemaContract:
    def test_no_duplicate_columns(self) -> None:
        cols = audit_cli._EXPORT_COLUMNS
        assert len(cols) == len(set(cols))

    @pytest.mark.parametrize("full", [True, False])
    def test_export_row_keys_match_columns(self, _isolated, full: bool) -> None:
        _record(run_id="r", full=full)
        keys = set(audit_cli._export_row(_row("r")).keys())
        assert keys == set(audit_cli._EXPORT_COLUMNS)

    def test_every_scorecard_axis_field_has_export_column(self) -> None:
        export_cols = set(audit_cli._EXPORT_COLUMNS)
        for _label, field, _unit, _fail, _missing in audit_cli._SCORECARD_AXES:
            assert field in export_cols, f"scorecard axis {field!r} missing from export schema"

    def test_csv_writer_roundtrips_full_row(self, _isolated) -> None:
        # End-to-end: a fully-populated row exports without DictWriter error
        # and every column header is present.
        import csv
        import io

        _record(run_id="rt", full=True)
        out = audit_cli.export("csv")
        reader = csv.DictReader(io.StringIO(out))
        assert list(reader.fieldnames) == list(audit_cli._EXPORT_COLUMNS)
        rows = list(reader)
        assert rows[0]["run_id"] == "rt"
