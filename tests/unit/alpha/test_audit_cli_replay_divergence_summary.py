"""Round 77: summary() reports the 驗證標準 §8 replay-divergence-category
distribution across rows below the strict 95% parity floor, so a reviewer sees
which inconsistency class (data_mismatch / latency_shift / …) dominates the
cohort's backtest↔replay failures. Audit-layer only, no relaxed thresholds."""

from __future__ import annotations

import pytest

from hft_platform.alpha import audit_cli, sub_gate_audit


def _record(
    *,
    run_id: str,
    match_pct: float | None,
    category: str | None = None,
) -> None:
    advisory: list[dict] = [
        {
            "name": "edge_per_round_trip",
            "passed": True,
            "metrics": {"mean_net_edge_pts_per_trade": 12.0},
            "details": "",
            "error": False,
        }
    ]
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


class TestSummaryReplayDivergenceDistribution:
    def test_below_floor_categories_counted_sorted_desc(self, _isolated) -> None:
        _record(run_id="a", match_pct=80.0, category="latency_shift")
        _record(run_id="b", match_pct=70.0, category="latency_shift")
        _record(run_id="c", match_pct=60.0, category="data_mismatch")
        out = audit_cli.summary()
        block = out.split("replay divergence (驗證標準 §8")[1]
        assert "3 rows" in block.split("\n")[0]
        body = [ln for ln in block.split("\n")[1:] if ln.strip()]
        # latency_shift (2) dominates, sorted before data_mismatch (1).
        assert body[0].strip().startswith("latency_shift")
        assert "latency_shift" in body[0] and ": 2" in body[0]
        assert any("data_mismatch" in ln and ": 1" in ln for ln in body)

    def test_above_floor_rows_excluded(self, _isolated) -> None:
        _record(run_id="ok", match_pct=99.0, category=None)
        out = audit_cli.summary()
        assert "replay divergence (驗證標準 §8" not in out

    def test_below_floor_missing_category_is_unknown(self, _isolated) -> None:
        _record(run_id="nocat", match_pct=80.0, category=None)
        out = audit_cli.summary()
        block = out.split("replay divergence (驗證標準 §8")[1]
        assert "unknown" in block

    def test_exactly_at_floor_not_below(self, _isolated) -> None:
        _record(run_id="edge", match_pct=95.0, category=None)
        out = audit_cli.summary()
        assert "replay divergence (驗證標準 §8" not in out
