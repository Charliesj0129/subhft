"""``audit parity-fields`` surfaces the per-FIELD replay-parity breakdown.

Goal §7 requires checking backtest↔replay agreement per field (signal time /
direction / size / entry / exit / session filter / risk filter / force-flat).
The existing ``divergence`` view collapses divergences to §8 categories, hiding
*which* intent field drifted. ``replay_parity`` now records the raw
``per_field_divergences`` histogram; this view tabulates ``field | divergences
| §8 category`` so an operator can pinpoint the drifting field.
"""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    strategy: str = "demo",
    instrument: str = "TXFD6",
    strategy_type: str = "maker",
    profile: str = "vm_ul6_strict",
    parity_metrics: dict | None = None,
) -> None:
    advisory: list[dict] = []
    if parity_metrics is not None:
        advisory.append({"name": "replay_parity", "passed": False, "metrics": parity_metrics, "details": ""})
    sub_gate_audit.record_sub_gate_run(
        run_id=run_id,
        strategy_name=strategy,
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


class TestAuditCliParityFields:
    def test_empty_audit_returns_no_match_message(self, _isolated) -> None:
        assert "no replay_parity rows match" in audit_cli.parity_fields()

    def test_skips_rows_without_replay_parity_entry(self, _isolated) -> None:
        _record(run_id="pf_none", parity_metrics=None)
        assert "no replay_parity rows match" in audit_cli.parity_fields()

    def test_tabulates_each_field_with_count_and_category(self, _isolated) -> None:
        _record(
            run_id="pf_one",
            strategy="alpha_one",
            parity_metrics={
                "match_pct": 80.0,
                "per_field_divergences": {"qty": 3, "session_phase": 5, "decision_price": 1},
            },
        )
        out = audit_cli.parity_fields()
        assert "pf_one" in out
        assert "alpha_one" in out
        # Each field maps to its §8 category.
        assert "qty" in out and "position_limit" in out
        assert "session_phase" in out and "session_phase_filter" in out
        assert "decision_price" in out and "latency_shift" in out
        # Sorted by divergence count descending: session_phase(5) before qty(3) before decision_price(1).
        lines = [ln for ln in out.split("\n") if ln.startswith("pf_one")]
        assert lines[0].split()[2] == "session_phase"
        assert lines[1].split()[2] == "qty"
        assert lines[2].split()[2] == "decision_price"

    def test_unknown_field_falls_back_to_unknown_category(self, _isolated) -> None:
        _record(
            run_id="pf_unknown",
            parity_metrics={"match_pct": 90.0, "per_field_divergences": {"mystery_field": 2}},
        )
        out = audit_cli.parity_fields()
        assert "mystery_field" in out
        assert "unknown" in out

    def test_legacy_row_without_per_field_data_is_noted_not_clean(self, _isolated) -> None:
        # Pre-capture row: has divergence_categories but no per_field_divergences.
        _record(
            run_id="pf_legacy",
            parity_metrics={"match_pct": 80.0, "divergence_categories": {"data_mismatch": 4}},
        )
        out = audit_cli.parity_fields()
        assert "predate per-field capture" in out
        assert "pf_legacy" in out

    def test_run_id_and_strategy_type_filters(self, _isolated) -> None:
        _record(
            run_id="pf_keep",
            strategy_type="maker",
            parity_metrics={"match_pct": 80.0, "per_field_divergences": {"price": 2}},
        )
        _record(
            run_id="pf_drop",
            strategy_type="taker",
            parity_metrics={"match_pct": 80.0, "per_field_divergences": {"price": 2}},
        )
        out = audit_cli.parity_fields("pf_keep", strategy_type="maker")
        assert "pf_keep" in out
        assert "pf_drop" not in out

    def test_main_dispatches_parity_fields_subcommand(self, _isolated, capsys) -> None:
        _record(
            run_id="pf_main",
            parity_metrics={"match_pct": 80.0, "per_field_divergences": {"session_phase": 1}},
        )
        rc = audit_cli.main(["parity-fields", "--run-id", "pf_main"])
        assert rc == 0
        captured = capsys.readouterr().out
        assert "pf_main" in captured
        assert "session_phase" in captured
        assert "session_phase_filter" in captured
