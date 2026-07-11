"""Governor CLI: `draft` writes unapproved briefs; `generate` is wired + fail-closed."""

from __future__ import annotations

import json

import pytest

from research.candidate_loop.__main__ import main


def _summary() -> dict:
    return {
        "run_id": "smoke_001",
        "per_family": {
            "trade_flow": {
                "candidates": 20,
                "survival_rate": 0.10,
                "ic_distribution_survivors": {"p10": 0.0, "p50": 0.114, "p90": 0.2},
                "cost_failure_rate": 0.55,
                "maker_cost_failure_rate": 0.40,
                "maker_rescuable_count": 2,
                "duplicate_rate": 0.05,
                "reduced_day_coverage_count": 7,
                "near_misses": [],
                "common_failure_patterns": [],
            }
        },
    }


def test_governor_draft_writes_unapproved_briefs(tmp_path, capsys):
    runs_root = tmp_path / "runs"
    (runs_root / "smoke_001").mkdir(parents=True)
    (runs_root / "smoke_001" / "failure_summary.json").write_text(json.dumps(_summary()))
    rc = main(
        [
            "governor",
            "draft",
            "--from-run",
            "smoke_001",
            "--runs-root",
            str(runs_root),
        ]
    )
    assert rc == 0
    brief = runs_root / "smoke_001" / "steering" / "trade_flow.md"
    assert "approved: false" in brief.read_text()


def test_governor_draft_missing_summary_returns_error(tmp_path):
    rc = main(["governor", "draft", "--from-run", "nope", "--runs-root", str(tmp_path)])
    assert rc == 2


def test_governor_generate_fails_closed_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    steering = tmp_path / "steering"
    steering.mkdir()
    with pytest.raises(SystemExit):
        main(
            [
                "governor",
                "generate",
                "--steering",
                str(steering),
                "--gen-run",
                "gen_001",
                "--candidates-root",
                str(tmp_path / "candidates"),
            ]
        )
