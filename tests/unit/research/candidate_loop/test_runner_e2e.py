"""Spec §17 E2E: 12-candidate fixture × synthetic 5-day TXFD6 inventory.

No ClickHouse (client=None): rows land in the jsonl fallback with the same
dedupe semantics, dir_coverage fail-closes to 0.0 (the trade_flow candidate
loses every day), and a re-run must add ZERO new rows (idempotency DoD).
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import pytest

from research.candidate_loop.generate import build_header, write_family_jsonl
from research.candidate_loop.runner import RunConfig, run_batch
from research.candidate_loop.schema import FAMILIES

REPO = Path(__file__).resolve().parents[4]
FIXTURE = REPO / "research" / "candidate_loop" / "fixtures" / "validator_matrix_12.jsonl"
PROMPTS_DIR = REPO / "research" / "candidate_loop" / "prompts" / "v1"

EVENT_DTYPE = np.dtype(
    [
        ("ev", "<u8"),
        ("exch_ts", "<i8"),
        ("local_ts", "<i8"),
        ("px", "<f8"),
        ("qty", "<f8"),
        ("order_id", "<u8"),
        ("ival", "<i8"),
        ("fval", "<f8"),
    ]
)
DEPTH, TRADE = 1, 2
BID, ASK = 1 << 29, 1 << 28
EXCH_LOCAL = (1 << 31) | (1 << 30)

TRAIN_DAYS = ("2026-01-04", "2026-01-05", "2026-01-06", "2026-01-07")
MISSING_DAY = "2026-01-04"  # in the split but no NPZ on disk -> missing_npz skip
VALIDATION_DAYS = ("2026-01-08",)
TEST_DAYS = ("2026-01-09",)

EVALUATOR_YAML = """
evaluator_version: eval_v1
primitive_version: prim_v1
latency_config_version: lat_shift_v1
cost_assumption_version: taifex_v1
tick_size:
  TXFD6: 1.0
cost_proxy:
  zscore_window: "200_events"
  hysteresis_sigma: 0.5
latency_shifts_ms: [0, 1]
horizon_decay_multipliers: [1.0, 2.0]
bucket_count: 5
dir_coverage_threshold: 0.95
min_valid_rows_per_day: 50
signal_std_epsilon: 1.0e-9
"""

SCORING_YAML = f"""
scoring_version: score_v1
gates:
  no_signal:
    signal_std_zero_day_fraction_max: 0.5
    train_ic_tstat_abs_min: 2.0
  sign_unstable:
    sign_consistency_min: 0.6
    contradiction_ic_floor: 0.01
  cost_proxy_taker:
    cost_survival_min: 0.3
  cost_proxy_maker:
    maker_cost_survival_min: 0.3
    q_hat_table: {REPO / "research" / "backtest" / "q_hat_data" / "txfd6_q_hat.parquet"}
    q_hat_symbol: TXFD6
    maker_cost_assumption_version: taifex_maker_qhat_v1
  latency_1ms:
    retention_min: 0.5
  one_day_only:
    one_day_concentration_max: 0.6
score_weights:
  predictive_score: 0.35
  stability_score: 0.15
  cost_survival_score: 0.20
  latency_survival_score: 0.10
  fragility_penalty: -0.10
  turnover_penalty: -0.05
  complexity_penalty: -0.05
normalization:
  ic_tstat_cap: 6.0
  cost_survival_cap: 2.0
  turnover_cap_flips_per_day: 500
  complexity_node_cap: 64
promotion:
  top_fraction: 0.01
  watchlist_next_decile: true
  watchlist_ic_tstat_min: 1.5
"""


def _split_yaml() -> str:
    def pairs(days: tuple[str, ...]) -> str:
        return "\n".join(f"    - {{day: '{d}', symbol: TXFD6}}" for d in days)

    return (
        "split_definition_version: split_v1\n"
        "data_version: synth_v1\n"
        "splits:\n"
        f"  train:\n{pairs(TRAIN_DAYS)}\n"
        f"  validation:\n{pairs(VALIDATION_DAYS)}\n"
        f"  test:\n{pairs(TEST_DAYS)}\n"
    )


def _synth_day(day_index: int, n_batches: int = 700) -> np.ndarray:
    """L5 book + trades; AR(1) imbalance weakly drives the next mid move."""
    rng = np.random.default_rng(1000 + day_index)
    day_start = (1_767_000_000 + day_index * 86_400) * 1_000_000_000
    rows: list[tuple] = []
    mid = 23_000.0
    imb = 0.0
    for k in range(n_batches):
        imb = 0.9 * imb + rng.normal(0.0, 1.0)
        mid += 0.4 * np.tanh(imb) + rng.normal(0.0, 0.8)
        ts = day_start + k * 100_000_000  # 100ms event clock
        lts = ts + 150_000  # 150us feed latency
        bid1 = np.floor(mid) - 0.0
        bid_qty = max(1.0, 20.0 + 8.0 * imb)
        ask_qty = max(1.0, 20.0 - 8.0 * imb)
        for lvl in range(5):
            rows.append((DEPTH | BID | EXCH_LOCAL, ts, lts, bid1 - lvl, bid_qty + lvl, 0, 0, 0.0))
            rows.append((DEPTH | ASK | EXCH_LOCAL, ts, lts, bid1 + 1 + lvl, ask_qty + lvl, 0, 0, 0.0))
        if k % 3 == 0:
            side = BID if imb > 0 else ASK
            rows.append((TRADE | side | EXCH_LOCAL, ts, lts, bid1, 2.0, 0, 0, 0.0))
    return np.array(rows, dtype=EVENT_DTYPE)


def _write_inventory(data_root: Path) -> None:
    days = [d for d in (*TRAIN_DAYS, *VALIDATION_DAYS, *TEST_DAYS) if d != MISSING_DAY]
    for i, day in enumerate(days):
        sym_dir = data_root / "txfd6"
        sym_dir.mkdir(parents=True, exist_ok=True)
        npz = sym_dir / f"TXFD6_{day}_l2.hftbt.npz"
        np.savez_compressed(npz, data=_synth_day(i))
        Path(str(npz) + ".meta.json").write_text(
            json.dumps(
                {
                    "data_fingerprint": f"synth_{day}",
                    "generator": "test_runner_e2e_synth",
                    "price_scale_applied": 1.0,
                }
            )
        )


def _write_batch(candidates_root: Path, run_id: str) -> None:
    """Split the committed fixture into per-family §11 files with real headers."""
    by_family: dict[str, list[str]] = defaultdict(list)
    for line in FIXTURE.read_text().splitlines():
        if not line.strip():
            continue
        family = str(json.loads(line).get("family", ""))
        if family not in FAMILIES:
            family = "microprice"  # off-contract candidates still need a file
        by_family[family].append(line)
    for family, lines in by_family.items():
        header = build_header(
            PROMPTS_DIR / f"{family}.md",
            generation_model="fixture_v1",
            generation_run_id=run_id,
            generated_at="2026-06-12T00:00:00+00:00",
        )
        write_family_jsonl(
            candidates_root / run_id / f"family={family}.jsonl", header, lines
        )


@pytest.fixture(scope="module")
def run_env(tmp_path_factory: pytest.TempPathFactory) -> dict:
    root = tmp_path_factory.mktemp("e2e")
    data_root = root / "data"
    _write_inventory(data_root)
    _write_batch(root / "candidates", "e2e_001")
    (root / "evaluator.yaml").write_text(EVALUATOR_YAML)
    (root / "scoring.yaml").write_text(SCORING_YAML)
    (root / "splits.yaml").write_text(_split_yaml())
    rc = RunConfig(
        run_id="e2e_001",
        batch_dir=root / "candidates" / "e2e_001",
        run_dir=root / "runs" / "e2e_001",
        data_root=data_root,
        artifact_root=root / "artifacts",
        evaluator_config_path=root / "evaluator.yaml",
        scoring_config_path=root / "scoring.yaml",
        split_definition_path=root / "splits.yaml",
    )
    summary = run_batch(rc, client=None)
    return {"rc": rc, "summary": summary, "root": root}


def _fallback_entries(rc: RunConfig) -> list[dict]:
    path = rc.run_dir / "_results_fallback.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestEndToEnd:
    def test_all_twelve_candidates_reach_terminal_status(self, run_env: dict) -> None:
        totals = run_env["summary"]["totals"]
        assert totals["candidates"] == 12
        assert totals["invalid"] == 7
        terminal = totals["invalid"] + totals["rejected"] + totals["watchlist"] + totals["promoted"]
        assert terminal == 12

    def test_gate_failed_rejections_carry_death_reason(self, run_env: dict) -> None:
        rows = [
            e["row"]
            for e in _fallback_entries(run_env["rc"])
            if e["table"] == "research.alpha_candidates" and e["row"]["status"] == "REJECTED"
        ]
        assert rows, "expected at least one gate-failed rejection"
        for row in rows:
            assert row["death_reason"], row["alpha_id"]

    def test_result_rows_have_non_null_maker_columns(self, run_env: dict) -> None:
        rows = [
            e["row"]
            for e in _fallback_entries(run_env["rc"])
            if e["table"] == "research.experiment_results"
        ]
        assert len(rows) >= 10  # 5 compiled x 2 splits (+ optional test rows)
        for row in rows:
            assert row["maker_cost_assumption_version"] == "taifex_maker_qhat_v1"
            assert isinstance(row["maker_cost_survival_score"], float)
            assert isinstance(row["maker_fill_prob_mean"], float)
            assert row["maker_required_move_threshold_pts"] >= 0.0

    def test_maker_view_never_more_pessimistic_than_taker(self, run_env: dict) -> None:
        rows = [
            e["row"]
            for e in _fallback_entries(run_env["rc"])
            if e["table"] == "research.experiment_results"
        ]
        for row in rows:
            assert (
                row["maker_required_move_threshold_pts"]
                <= row["required_move_threshold_pts"] + 1e-12
            )

    def test_trade_flow_candidate_loses_all_days_without_ch(self, run_env: dict) -> None:
        """client=None -> dir_coverage fail-closed 0.0 -> every day dir_dirty."""
        rows = [
            e["row"]
            for e in _fallback_entries(run_env["rc"])
            if e["table"] == "research.experiment_results" and e["row"]["family"] == "trade_flow"
        ]
        assert rows
        for row in rows:
            assert row["effective_day_count"] == 0

    def test_missing_npz_day_reduces_effective_day_count(self, run_env: dict) -> None:
        rows = [
            e["row"]
            for e in _fallback_entries(run_env["rc"])
            if e["table"] == "research.experiment_results"
            and e["row"]["split"] == "train"
            and e["row"]["family"] != "trade_flow"
        ]
        assert rows
        for row in rows:
            assert row["day_count"] == len(TRAIN_DAYS)
            assert row["effective_day_count"] == len(TRAIN_DAYS) - 1

    def test_failure_summary_written_train_validation_only(self, run_env: dict) -> None:
        path = run_env["rc"].run_dir / "failure_summary.json"
        summary = json.loads(path.read_text())
        assert summary["splits_included"] == ["train", "validation"]
        assert summary["run_id"] == "e2e_001"
        assert summary["versions"]["maker_cost_assumption_version"] == "taifex_maker_qhat_v1"
        assert summary["prompt_ids_used"]  # provenance flowed through from headers

    def test_test_split_rows_only_for_watchlist_or_promoted(self, run_env: dict) -> None:
        entries = _fallback_entries(run_env["rc"])
        shortlist = {
            e["row"]["alpha_id"]
            for e in entries
            if e["table"] == "research.alpha_candidates"
            and e["row"]["status"] in ("WATCHLIST", "PROMOTED")
        }
        test_rows = {
            e["row"]["alpha_id"]
            for e in entries
            if e["table"] == "research.experiment_results" and e["row"]["split"] == "test"
        }
        assert test_rows == shortlist

    def test_split_artifacts_written_per_candidate_split(self, run_env: dict) -> None:
        rows = [
            e["row"]
            for e in _fallback_entries(run_env["rc"])
            if e["table"] == "research.experiment_results"
        ]
        for row in rows:
            artifact_dir = Path(row["artifact_path"])
            assert (artifact_dir / "day_metrics.parquet").exists()
            assert (artifact_dir / "diagnostics.json").exists()

    def test_rerun_is_idempotent_zero_new_rows(self, run_env: dict) -> None:
        rc = run_env["rc"]
        before = len(_fallback_entries(rc))
        summary2 = run_batch(rc, client=None)
        after = len(_fallback_entries(rc))
        assert after == before
        assert summary2["totals"] == run_env["summary"]["totals"]
