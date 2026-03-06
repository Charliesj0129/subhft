from __future__ import annotations

import argparse
import json
from pathlib import Path

from research.tools import paper_trade


def test_record_and_summarize_paper_trade(tmp_path: Path) -> None:
    base = tmp_path / "experiments"
    rc = paper_trade.cmd_record_paper(
        argparse.Namespace(
            alpha_id="ofi_mc",
            experiments_dir=str(base),
            session_id="s1",
            started_at="2026-02-20T09:00:00+00:00",
            ended_at="2026-02-20T13:30:00+00:00",
            trading_day="2026-02-20",
            fills=12,
            pnl_bps=3.4,
            drift_alerts=0,
            execution_reject_rate=0.001,
            reject_rate_p95=0.002,
            regime="trending",
            notes="good",
        )
    )
    assert rc == 0

    out = tmp_path / "summary.json"
    rc = paper_trade.cmd_summarize_paper(
        argparse.Namespace(
            alpha_id="ofi_mc",
            experiments_dir=str(base),
            out=str(out),
        )
    )
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["session_count"] == 1
    assert payload["distinct_trading_days"] == 1
    assert payload["calendar_span_days"] == 1


def test_check_paper_governance_passes_in_strict_mode(tmp_path: Path) -> None:
    base = tmp_path / "experiments"
    for idx, day in enumerate(("2026-03-01", "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"), start=1):
        rc = paper_trade.cmd_record_paper(
            argparse.Namespace(
                alpha_id="ofi_mc",
                experiments_dir=str(base),
                session_id=f"s{idx}",
                started_at=f"{day}T09:00:00+00:00",
                ended_at=f"{day}T10:30:00+00:00",
                trading_day=day,
                fills=10,
                pnl_bps=1.0,
                drift_alerts=0,
                execution_reject_rate=0.001,
                reject_rate_p95=0.002,
                regime=("trending" if idx % 2 == 0 else "mean_reverting"),
                notes="ok",
            )
        )
        assert rc == 0

    out = tmp_path / "governance.json"
    rc = paper_trade.cmd_check_paper_governance(
        argparse.Namespace(
            alpha_id="ofi_mc",
            experiments_dir=str(base),
            min_shadow_sessions=5,
            min_calendar_days=5,
            min_trading_days=5,
            min_session_minutes=60,
            max_drift_alerts=0,
            max_execution_reject_rate=0.01,
            min_regimes=2,
            strict=True,
            out=str(out),
        )
    )
    assert rc == 0
    payload = json.loads(out.read_text())
    assert payload["passed"] is True
    assert payload["checks"]["execution_reject_rate"]["source"] == "p95"


def test_check_paper_governance_strict_fails_when_threshold_not_met(tmp_path: Path) -> None:
    base = tmp_path / "experiments"
    rc = paper_trade.cmd_record_paper(
        argparse.Namespace(
            alpha_id="ofi_mc",
            experiments_dir=str(base),
            session_id="s1",
            started_at="2026-03-05T09:00:00+00:00",
            ended_at="2026-03-05T10:00:00+00:00",
            trading_day="2026-03-05",
            fills=3,
            pnl_bps=-1.2,
            drift_alerts=2,
            execution_reject_rate=0.03,
            reject_rate_p95=0.04,
            regime="trending",
            notes="noisy",
        )
    )
    assert rc == 0

    rc = paper_trade.cmd_check_paper_governance(
        argparse.Namespace(
            alpha_id="ofi_mc",
            experiments_dir=str(base),
            min_shadow_sessions=5,
            min_calendar_days=7,
            min_trading_days=5,
            min_session_minutes=60,
            max_drift_alerts=0,
            max_execution_reject_rate=0.01,
            min_regimes=2,
            strict=True,
            out=None,
        )
    )
    assert rc == 2
