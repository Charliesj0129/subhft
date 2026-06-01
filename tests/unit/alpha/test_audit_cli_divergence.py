"""Round 30: ``audit divergence`` surfaces replay-parity classification.

Goal §7/§8 require parity checks and a canonical divergence taxonomy.
The replay_parity sub-gate already emits ``dominant_divergence_category``
and ``divergence_categories`` in each row's metrics; this CLI surface
tabulates them so an operator can see which runs diverge and into which
bucket without parsing JSONL.
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
    parity: dict | None = None,
) -> None:
    """Inject a row whose advisory already contains a replay_parity entry."""
    advisory: list[dict] = []
    if parity is not None:
        advisory.append({"name": "replay_parity", **parity})
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


def _parity(
    match_pct: float,
    first_idx: int,
    categories: dict[str, int],
    passed: bool | None = None,
) -> dict:
    dominant = max(categories, key=categories.get) if categories else ""
    return {
        "passed": match_pct >= 95.0 if passed is None else passed,
        "metrics": {
            "match_pct": match_pct,
            "first_divergence_idx": first_idx,
            "divergence_categories": categories,
            "dominant_divergence_category": dominant,
        },
        "details": "",
    }


@pytest.fixture
def _isolated(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("HFT_SUB_GATE_AUDIT_PATH", str(path))
    sub_gate_audit._reset_cache_for_tests()
    return path


class TestAuditCliDivergence:
    def test_empty_audit_returns_no_match_message(self, _isolated) -> None:
        assert "no audit rows match" in audit_cli.divergence()

    def test_skips_rows_without_replay_parity_entry(self, _isolated) -> None:
        _record(run_id="d_no_parity", parity=None)
        assert "no audit rows match" in audit_cli.divergence()

    def test_tabulates_match_pct_first_idx_and_dominant_category(self, _isolated) -> None:
        _record(
            run_id="d_one",
            strategy="alpha_one",
            parity=_parity(
                match_pct=82.5,
                first_idx=42,
                categories={"data_mismatch": 7, "feature_mismatch": 2},
            ),
        )
        out = audit_cli.divergence()
        assert "d_one" in out
        assert "alpha_one" in out
        assert "82.50" in out
        assert "42" in out
        assert "data_mismatch" in out
        assert "data_mismatch=7" in out

    def test_first_idx_negative_renders_none(self, _isolated) -> None:
        _record(
            run_id="d_perfect",
            parity=_parity(match_pct=100.0, first_idx=-1, categories={}, passed=True),
        )
        out = audit_cli.divergence()
        assert "(none)" in out

    def test_category_filter_keeps_matching_rows_only(self, _isolated) -> None:
        _record(
            run_id="d_data",
            parity=_parity(85.0, 10, {"data_mismatch": 5}),
        )
        _record(
            run_id="d_lat",
            parity=_parity(85.0, 12, {"latency_shift": 8}),
        )
        out = audit_cli.divergence(category="latency_shift")
        assert "d_lat" in out
        assert "d_data" not in out
        assert "(1 row)" in out

    def test_only_failed_filter_drops_passing_rows(self, _isolated) -> None:
        _record(
            run_id="d_pass",
            parity=_parity(99.0, -1, {}, passed=True),
        )
        _record(
            run_id="d_fail",
            parity=_parity(80.0, 5, {"timestamp_alignment_error": 3}, passed=False),
        )
        out = audit_cli.divergence(only_failed=True)
        assert "d_fail" in out
        assert "d_pass" not in out

    def test_strategy_type_and_profile_filter(self, _isolated) -> None:
        _record(
            run_id="d_m_strict",
            strategy_type="maker",
            profile="vm_ul6_strict",
            parity=_parity(80.0, 5, {"data_mismatch": 1}, passed=False),
        )
        _record(
            run_id="d_t_strict",
            strategy_type="taker",
            profile="vm_ul6_strict",
            parity=_parity(80.0, 5, {"data_mismatch": 1}, passed=False),
        )
        _record(
            run_id="d_m_loose",
            strategy_type="maker",
            profile="loose",
            parity=_parity(80.0, 5, {"data_mismatch": 1}, passed=False),
        )
        out = audit_cli.divergence(strategy_type="maker", profile="vm_ul6_strict")
        assert "d_m_strict" in out
        assert "d_t_strict" not in out
        assert "d_m_loose" not in out

    def test_top_categories_sorted_descending_and_truncated_to_three(self, _isolated) -> None:
        _record(
            run_id="d_many",
            parity=_parity(
                70.0,
                3,
                {
                    "data_mismatch": 1,
                    "feature_mismatch": 5,
                    "latency_shift": 3,
                    "session_phase_filter": 2,
                    "unknown": 4,
                },
            ),
        )
        out = audit_cli.divergence()
        # Top 3 by count: feature_mismatch=5, unknown=4, latency_shift=3.
        assert "feature_mismatch=5,unknown=4,latency_shift=3" in out
        # The two lower-count buckets must NOT appear in the top_categories cell.
        # (Could legitimately appear elsewhere, but in a single-row table they
        # wouldn't.)  Assert by checking the rendered row line does not include
        # them.
        rendered = [ln for ln in out.split("\n") if ln.startswith("d_many")]
        assert rendered, out
        assert "data_mismatch=1" not in rendered[0]
        assert "session_phase_filter=2" not in rendered[0]

    def test_main_dispatches_divergence_subcommand(self, _isolated, capsys) -> None:
        _record(
            run_id="d_main",
            parity=_parity(80.0, 4, {"data_mismatch": 2}, passed=False),
        )
        rc = audit_cli.main(["divergence", "--only-failed"])
        assert rc == 0
        captured = capsys.readouterr().out
        assert "d_main" in captured
        assert "data_mismatch=2" in captured
