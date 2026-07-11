"""T1-E open-gap overreaction fade V0 detector + hard gate.

Exercises the endogenous prior-close -> today-open gap construction, the FADE
direction, the 8-pt net-cost deduction, and the hard-gate / IS-OOS summarizer.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from hft_platform.alpha.strategy_spec import load_spec, validate_spec
from research.t1.regime_viability import (
    OpenGapFadeConfig,
    audit_open_gap_fade_pair,
    detect_open_gap_fade_events,
    extract_bbo_and_trades,
    run_open_gap_fade_audit,
    summarize_open_gap_fade_rows,
)

HFTBT_DTYPE = np.dtype(
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

BID_DEPTH = np.uint64(0xE0000002)
ASK_DEPTH = np.uint64(0xD0000002)

NS_PER_MINUTE = 60 * 1_000_000_000
SPEC_PATH = Path("research/alphas/t1e_txf_opengap_fade_tmf/spec.yaml")


def session_ns(date: str, minute: float) -> int:
    # Local 08:45 TPE == 00:45 UTC; matches _session_start_ns(tz_offset=8).
    base = datetime.fromisoformat(f"{date}T00:45:00+00:00")
    return int(base.timestamp() * 1_000_000_000) + int(minute * NS_PER_MINUTE)


def _quote(ts: int, mid: float) -> list[tuple]:
    return [
        (BID_DEPTH, ts, ts, mid - 0.5, 1.0, 0, 0, 0.0),
        (ASK_DEPTH, ts, ts, mid + 0.5, 1.0, 0, 0, 0.0),
    ]


def _clear(ts: int, mid: float) -> list[tuple]:
    return [
        (BID_DEPTH, ts, ts, mid - 0.5, 0.0, 0, 0, 0.0),
        (ASK_DEPTH, ts, ts, mid + 0.5, 0.0, 0, 0, 0.0),
    ]


def _session_array(date: str, points: list[tuple[float, float]]) -> np.ndarray:
    rows: list[tuple] = []
    prev: float | None = None
    for minute, mid in points:
        ts = session_ns(date, minute)
        if prev is not None:
            rows.extend(_clear(ts, prev))
        rows.extend(_quote(ts, mid))
        prev = mid
    return np.array(rows, dtype=HFTBT_DTYPE)


def _config(today: str, prior: str) -> OpenGapFadeConfig:
    from research.t1.regime_viability import _session_start_ns

    return OpenGapFadeConfig(
        session_start_ns=_session_start_ns(today),
        prior_session_start_ns=_session_start_ns(prior),
        session_minutes=300,
        prior_close_window_minutes=30,
        open_confirm_minutes=15,
        min_gap_pts=15.0,
        stop_buffer_pts=15.0,
    )


# Prior session closes ~17000 over its final 30 min (270-300m).
_PRIOR = [(1, 16980.0), (100, 16990.0), (275, 17000.0), (285, 17000.0), (295, 17000.0)]


def test_detects_short_fade_on_gap_up():
    prior = extract_bbo_and_trades(_session_array("2026-03-02", _PRIOR))[0]
    # Today opens 40pt above prior close -> outsized gap up -> FADE short.
    today_bbo, today_trades = extract_bbo_and_trades(
        _session_array("2026-03-03", [(0, 17040.0), (5, 17042.0), (15, 17041.0), (30, 17035.0), (60, 17020.0)])
    )

    events = detect_open_gap_fade_events(
        prior, today_bbo, today_trades, contract="TXFC6", date="2026-03-03",
        config=_config("2026-03-03", "2026-03-02"),
    )

    assert len(events) == 1
    ev = events[0]
    assert ev.regime_type == "T1-E_open_gap_overreaction_fade"
    assert ev.direction == -1  # fade the gap up
    assert ev.realized_vol_ratio > 15.0  # signed gap carried in reused field
    assert ev.trigger_time_ns == session_ns("2026-03-03", 15)


def test_detects_long_fade_on_gap_down():
    prior = extract_bbo_and_trades(_session_array("2026-03-02", _PRIOR))[0]
    today_bbo, today_trades = extract_bbo_and_trades(
        _session_array("2026-03-03", [(0, 16960.0), (5, 16958.0), (15, 16959.0), (30, 16965.0), (60, 16980.0)])
    )

    events = detect_open_gap_fade_events(
        prior, today_bbo, today_trades, contract="TXFC6", date="2026-03-03",
        config=_config("2026-03-03", "2026-03-02"),
    )

    assert len(events) == 1
    assert events[0].direction == 1  # fade the gap down


def test_no_event_when_gap_below_threshold():
    prior = extract_bbo_and_trades(_session_array("2026-03-02", _PRIOR))[0]
    # Today opens only 5pt above prior close -> below 15pt threshold.
    today_bbo, today_trades = extract_bbo_and_trades(
        _session_array("2026-03-03", [(0, 17005.0), (5, 17004.0), (15, 17006.0), (30, 17005.0), (60, 17007.0)])
    )

    events = detect_open_gap_fade_events(
        prior, today_bbo, today_trades, contract="TXFC6", date="2026-03-03",
        config=_config("2026-03-03", "2026-03-02"),
    )

    assert events == []


def test_pair_audit_deducts_round_trip_cost_from_gross_return(tmp_path):
    prior_txf = tmp_path / "TXFC6_2026-03-02_l2.hftbt.npz"
    today_txf = tmp_path / "TXFC6_2026-03-03_l2.hftbt.npz"
    today_tmf = tmp_path / "TMFC6_2026-03-03_l2.hftbt.npz"
    np.savez(prior_txf, data=_session_array("2026-03-02", _PRIOR))
    # Gap up -> short. Today TXF stays below the 17055 gap-extension stop (no
    # breach); TMF reverts down so a short profits on the executable exit path.
    np.savez(
        today_txf,
        data=_session_array(
            "2026-03-03", [(0, 17040.0), (5, 17041.0), (15, 17041.0), (30, 17039.0), (45, 17038.0), (60, 17036.0)]
        ),
    )
    np.savez(
        today_tmf,
        data=_session_array(
            "2026-03-03", [(15, 17040.0), (20, 17038.0), (30, 17030.0), (40, 17025.0), (44, 17020.0)]
        ),
    )

    rows = audit_open_gap_fade_pair(
        prior_txf_path=prior_txf,
        today_txf_path=today_txf,
        today_tmf_path=today_tmf,
        cost_pts=8.0,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["direction"] == -1
    assert row["net_30m_pts"] is not None
    assert row["net_after_cost_30m"] == row["net_30m_pts"] - 8.0
    assert row["cost_pts"] == 8.0
    assert row["prior_date"] == "2026-03-02"


def test_run_audit_pairs_consecutive_days_and_writes_summary(tmp_path):
    raw_dir = tmp_path / "raw"
    out_dir = tmp_path / "validations" / "t1e"
    txf_dir = raw_dir / "txfc6"
    tmf_dir = raw_dir / "tmfc6"
    txf_dir.mkdir(parents=True)
    tmf_dir.mkdir(parents=True)
    np.savez(txf_dir / "TXFC6_2026-03-02_l2.hftbt.npz", data=_session_array("2026-03-02", _PRIOR))
    np.savez(
        txf_dir / "TXFC6_2026-03-03_l2.hftbt.npz",
        data=_session_array("2026-03-03", [(0, 17040.0), (5, 17041.0), (15, 17041.0), (30, 17039.0), (45, 17038.0), (60, 17036.0)]),
    )
    np.savez(
        tmf_dir / "TMFC6_2026-03-03_l2.hftbt.npz",
        data=_session_array("2026-03-03", [(15, 17040.0), (20, 17038.0), (30, 17030.0), (40, 17025.0), (44, 17020.0)]),
    )

    summary = run_open_gap_fade_audit(
        SimpleNamespace(
            raw_dir=str(raw_dir),
            out_dir=str(out_dir),
            months="C6",
            max_date=None,
            min_date=None,
            max_pairs=None,
            session_tz_offset_hours=8,
            cost_pts=8.0,
            session_minutes=300,
            prior_close_window_minutes=30,
            open_confirm_minutes=15,
            min_gap_pts=15.0,
            stop_buffer_pts=15.0,
            oos_start="2026-03-03",
            edge_floor_pts=10.0,
        )
    )

    persisted = json.loads(Path(summary["summary_path"]).read_text(encoding="utf-8"))
    assert persisted["candidate"] == "t1e_txf_opengap_fade_tmf"
    assert persisted["artifact_scope"] == "validation_summary"
    assert persisted["splits"]["full"]["events"] == 1


def test_t1e_candidate_has_governed_fixed_spec():
    spec = load_spec(SPEC_PATH)

    assert validate_spec(spec) == []
    assert spec["strategy_name"] == "t1e_txf_opengap_fade_tmf"
    assert spec["validation_plan"]["net_edge_floor_pts"] >= 10.0


def test_summarizer_kills_on_negative_median_net():
    rows = [
        {
            "contract": f"TXF{c}6->TMF{c}6",
            "date": f"2026-0{d}",
            "net_after_cost_30m": -5.0,
            "net_30m_pts": 3.0,
            "stop_structure_breached": True,
        }
        for c in ("B", "C", "D", "E")
        for d in ("1-05", "2-05", "3-05")
    ]
    summary = summarize_open_gap_fade_rows(rows)

    assert summary["verdict"] == "KILL"
    assert summary["hard_gate"]["median_net_positive"] is False
