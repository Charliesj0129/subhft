"""Shared fixtures for t1_a_zero_event_diagnostic tests."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

COVERAGE_COLUMNS = [
    "contract",
    "trading_day",
    "pair_id",
    "or_start",
    "or_end",
    "bbo_first_time",
    "bbo_last_time",
    "coverage_status",
    "or_high",
    "or_low",
    "or_width",
    "post_or_high",
    "post_or_low",
    "max_upside_break_pts",
    "max_downside_break_pts",
    "first_up_break_time",
    "first_down_break_time",
    "break_side",
    "break_magnitude_pts",
    "break_magnitude_vs_or_width",
    "break_magnitude_vs_prior_realized_vol",
    "vwap_side_at_break",
    "reverted_to_or",
    "time_above_or_high",
    "time_below_or_low",
    "event_selected_by_v0",
    "persistent_after_break",
    "realized_vol_ratio",
]


def coverage_row(**overrides) -> dict:
    """Build a coverage row with sensible defaults; overrides win."""
    base = {
        "contract": "TXFD6",
        "trading_day": "2026-04-01",
        "pair_id": "TXFD6->TMFD6",
        "or_start": "2026-04-01T00:45:00+00:00",
        "or_end": "2026-04-01T01:15:00+00:00",
        "bbo_first_time": "2026-04-01T00:45:01+00:00",
        "bbo_last_time": "2026-04-01T05:45:00+00:00",
        "coverage_status": "ok",
        "or_high": 17050.0,
        "or_low": 17000.0,
        "or_width": 50.0,
        "post_or_high": 17070.0,
        "post_or_low": 16980.0,
        "max_upside_break_pts": 20.0,
        "max_downside_break_pts": 0.0,
        "first_up_break_time": "2026-04-01T01:20:00+00:00",
        "first_down_break_time": None,
        "break_side": "up",
        "break_magnitude_pts": 3.0,
        "break_magnitude_vs_or_width": 0.06,
        "break_magnitude_vs_prior_realized_vol": 1.5,
        "vwap_side_at_break": "above",
        "reverted_to_or": False,
        "time_above_or_high": 600,
        "time_below_or_low": 0,
        "event_selected_by_v0": False,
        "persistent_after_break": True,
        "realized_vol_ratio": 1.40,
    }
    base.update(overrides)
    return base


@pytest.fixture
def make_coverage_csv(tmp_path: Path):
    def _make(rows: list[dict], name: str = "coverage.csv") -> Path:
        df = pd.DataFrame(rows, columns=COVERAGE_COLUMNS)
        p = tmp_path / name
        df.to_csv(p, index=False)
        return p

    return _make


@pytest.fixture
def viability_event_csv(tmp_path: Path):
    """Build a viability event CSV with N rows for A5 cross-check."""
    def _make(n_events: int, name: str = "events.csv") -> Path:
        p = tmp_path / name
        if n_events == 0:
            p.write_text("", encoding="utf-8")
        else:
            df = pd.DataFrame({"event_idx": list(range(n_events))})
            df.to_csv(p, index=False)
        return p

    return _make
