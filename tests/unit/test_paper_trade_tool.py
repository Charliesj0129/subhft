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
