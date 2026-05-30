"""Round 88: residual mark-to-market (驗證標準 §2/§3 殘倉 mark-to-market) travels
with the unified-metrics export.  show() surfaces residual_mtm (Round 66); the
export must carry residual_mtm_pts + inventory_net_pts too so an external pivot
can see whether un-FIFO'd inventory props the edge up.  Audit-layer only."""

from __future__ import annotations

import csv
import io

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(*, run_id: str, with_inventory: bool) -> None:
    advisory: list[dict] = [
        {
            "name": "edge_per_round_trip",
            "passed": True,
            "metrics": {"mean_net_edge_pts_per_trade": 12.0},
            "details": "",
        }
    ]
    if with_inventory:
        advisory.append(
            {
                "name": "inventory_mtm",
                "passed": True,
                "metrics": {
                    "realized_pts": 8.0,
                    "residual_mtm_pts": 4.0,
                    "net_pts": 12.0,
                },
                "details": "",
            }
        )
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name=f"demo_{run_id}",
        instrument="TXFD6",
        strategy_type="taker",
        profile_name="vm_ul6_strict",
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


def _csv_rows(out: str):
    reader = csv.DictReader(io.StringIO(out))
    return {r["run_id"]: r for r in reader}, reader.fieldnames


class TestResidualMtmExportColumns:
    def test_columns_in_header(self, _isolated) -> None:
        _record(run_id="h", with_inventory=True)
        _rows, fieldnames = _csv_rows(audit_cli.export("csv"))
        assert "residual_mtm_pts" in fieldnames
        assert "inventory_net_pts" in fieldnames

    def test_values_populated_when_gate_ran(self, _isolated) -> None:
        _record(run_id="inv", with_inventory=True)
        rows, _ = _csv_rows(audit_cli.export("csv"))
        r = rows["inv"]
        assert r["residual_mtm_pts"] == "4.0000"
        assert r["inventory_net_pts"] == "12.0000"

    def test_empty_when_gate_absent(self, _isolated) -> None:
        _record(run_id="bare", with_inventory=False)
        rows, _ = _csv_rows(audit_cli.export("csv"))
        r = rows["bare"]
        assert r["residual_mtm_pts"] == ""
        assert r["inventory_net_pts"] == ""

    def test_export_row_keys_still_match_columns(self, _isolated) -> None:
        # Schema-contract invariant must still hold after the additions.
        _record(run_id="k", with_inventory=True)
        row = sub_gate_audit.read_runs("k")[0]
        assert set(audit_cli._export_row(row).keys()) == set(audit_cli._EXPORT_COLUMNS)
