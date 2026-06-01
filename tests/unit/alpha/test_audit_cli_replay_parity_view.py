"""Round 62: replay-parity match_pct (驗證標準 §7) + dominant divergence
category (驗證標準 §8) lifted from the replay_parity gate to top-level row
fields and surfaced in `audit show` with PASS/FAIL vs the strict 95.0% floor.
Audit-layer only, no relaxed thresholds."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    match_pct: float | None,
    category: str | None = None,
    edge: float | None = 12.0,
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
    if match_pct is not None:
        metrics: dict[str, object] = {
            "match_pct": match_pct,
            "threshold": 95.0,
            "first_divergence_idx": -1.0 if match_pct >= 95.0 else 3.0,
        }
        if category is not None:
            metrics["dominant_divergence_category"] = category
        advisory.append(
            {
                "name": "replay_parity",
                "passed": match_pct >= 95.0,
                "metrics": metrics,
                "details": "",
                "error": False,
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


class TestBuildRecordLiftsReplayParity:
    def test_match_pct_lifted(self, _isolated) -> None:
        _record(run_id="a", match_pct=99.0)
        rows = sub_gate_audit.read_runs()
        assert rows[0]["replay_match_pct"] == 99.0

    def test_category_lifted_when_present(self, _isolated) -> None:
        _record(run_id="c", match_pct=80.0, category="latency_shift")
        rows = sub_gate_audit.read_runs()
        assert rows[0]["replay_divergence_category"] == "latency_shift"

    def test_all_vocab_categories_lift_verbatim(self, _isolated) -> None:
        # Every §8 category must round-trip unchanged.
        vocab = [
            "data_mismatch",
            "feature_mismatch",
            "timestamp_alignment_error",
            "latency_shift",
            "session_phase_filter",
            "risk_filter",
            "position_limit",
            "implementation_drift",
            "unknown",
        ]
        for i, cat in enumerate(vocab):
            _record(run_id=f"v{i}", match_pct=80.0, category=cat)
        rows = {r["run_id"]: r for r in sub_gate_audit.read_runs()}
        for i, cat in enumerate(vocab):
            assert rows[f"v{i}"]["replay_divergence_category"] == cat

    def test_out_of_vocab_category_collapses_to_unknown(self, _isolated) -> None:
        _record(run_id="oov", match_pct=80.0, category="some_made_up_label")
        rows = sub_gate_audit.read_runs()
        assert rows[0]["replay_divergence_category"] == "unknown"

    def test_absent_when_gate_didnt_run(self, _isolated) -> None:
        _record(run_id="b", match_pct=None)
        rows = sub_gate_audit.read_runs()
        assert "replay_match_pct" not in rows[0]
        assert "replay_divergence_category" not in rows[0]


class TestShowSurfacesReplayParity:
    def test_above_floor_reads_pass(self, _isolated) -> None:
        _record(run_id="s_ok", match_pct=99.0)
        out = audit_cli.show("s_ok")
        line = out.split("replay_parity")[1].split("\n")[0]
        assert "99.0%" in line
        assert "PASS" in line

    def test_below_floor_reads_fail_with_category(self, _isolated) -> None:
        _record(run_id="s_bad", match_pct=80.0, category="latency_shift")
        out = audit_cli.show("s_bad")
        line = out.split("replay_parity")[1].split("\n")[0]
        assert "80.0%" in line
        assert "FAIL" in line
        assert "dominant=latency_shift" in line

    def test_exactly_floor_reads_pass(self, _isolated) -> None:
        _record(run_id="s_edge", match_pct=95.0)
        out = audit_cli.show("s_edge")
        line = out.split("replay_parity")[1].split("\n")[0]
        assert "PASS" in line

    def test_na_when_missing(self, _isolated) -> None:
        _record(run_id="s_na", match_pct=None)
        out = audit_cli.show("s_na")
        assert "replay_parity  : (n/a" in out
