"""T1-D intraday-session-momentum V0 detector + hard gate.

Mirrors the synthetic-event style of ``test_t1b_vol_compression`` but exercises
the open-window -> last-window momentum mechanism (Gao-Han-Li-Zhou, JFE 2018),
the 8-pt net-cost deduction, and the hard-gate / IS-OOS summarizer.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from hft_platform.alpha.strategy_spec import load_spec, validate_spec
from research.t1.regime_viability import (
    IntradayMomentumConfig,
    audit_intraday_momentum_pair,
    detect_intraday_momentum_events,
    extract_bbo_and_trades,
    run_intraday_momentum_audit,
    summarize_intraday_momentum_rows,
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
ASK_TRADE = np.uint64(0xD0000001)

NS_PER_MINUTE = 60 * 1_000_000_000
SPEC_PATH = Path("research/alphas/t1d_txf_intraday_momentum_tmf/spec.yaml")


def ns_at(minute: float) -> int:
    base = datetime(2026, 3, 3, 0, 45, 0, tzinfo=timezone.utc)
    return int(base.timestamp() * 1_000_000_000) + int(minute * NS_PER_MINUTE)


def _quote(ts: int, bid: float, ask: float, qty: float = 1.0) -> list[tuple]:
    return [
        (BID_DEPTH, ts, ts, bid, qty, 0, 0, 0.0),
        (ASK_DEPTH, ts, ts, ask, qty, 0, 0, 0.0),
    ]


def _clear(ts: int, bid: float, ask: float) -> list[tuple]:
    return [
        (BID_DEPTH, ts, ts, bid, 0.0, 0, 0, 0.0),
        (ASK_DEPTH, ts, ts, ask, 0.0, 0, 0, 0.0),
    ]


def _trade(ts: int, px: float, qty: float = 2.0) -> tuple:
    return (ASK_TRADE, ts, ts, px, qty, 0, 0, 0.0)


def _build_morning_momentum(*, direction: int) -> np.ndarray:
    """Open window 0-30m establishes a >=10pt directional move; it persists into
    the predict window 90-120m. Session length 120m -> entry at minute 90."""
    rows: list[tuple] = []
    prev: tuple[float, float] | None = None

    def push(minute: float, mid: float, *, trade: bool = False) -> None:
        nonlocal prev
        ts = ns_at(minute)
        if prev is not None:
            rows.extend(_clear(ts, prev[0], prev[1]))
        rows.extend(_quote(ts, mid - 0.5, mid + 0.5))
        if trade:
            rows.append(_trade(ts + 1_000_000_000, mid))
        prev = (mid - 0.5, mid + 0.5)

    if direction > 0:
        open_mids = [(1, 100.0), (5, 105.0), (10, 110.0), (15, 114.0), (20, 117.0), (25, 120.0)]
        mid_mids = [(35, 120.0), (45, 121.0), (55, 120.0), (65, 121.0), (75, 120.0), (85, 121.0)]
        predict_mids = [(90, 122.0), (95, 123.0), (100, 124.0), (105, 125.0), (110, 126.0), (115, 127.0)]
    else:
        open_mids = [(1, 120.0), (5, 115.0), (10, 110.0), (15, 106.0), (20, 103.0), (25, 100.0)]
        mid_mids = [(35, 100.0), (45, 99.0), (55, 100.0), (65, 99.0), (75, 100.0), (85, 99.0)]
        predict_mids = [(90, 98.0), (95, 97.0), (100, 96.0), (105, 95.0), (110, 94.0), (115, 93.0)]

    for minute, mid in open_mids:
        push(minute, mid)
    for minute, mid in mid_mids:
        push(minute, mid, trade=True)
    for minute, mid in predict_mids:
        push(minute, mid)
    return np.array(rows, dtype=HFTBT_DTYPE)


def _config() -> IntradayMomentumConfig:
    return IntradayMomentumConfig(
        session_start_ns=ns_at(0),
        session_minutes=120,
        open_window_minutes=30,
        predict_window_minutes=30,
        min_open_move_pts=10.0,
    )


def test_t1d_candidate_has_governed_fixed_spec():
    spec = load_spec(SPEC_PATH)

    errors = validate_spec(spec)

    assert errors == []
    assert spec["strategy_name"] == "t1d_txf_intraday_momentum_tmf"
    assert spec["validation_plan"]["net_edge_floor_pts"] >= 10.0
    assert "edge_per_round_trip" in spec["validation_plan"]["required_gates"]
    assert "replay_parity" in spec["validation_plan"]["required_gates"]


def test_detects_long_momentum_from_up_morning():
    bbo, trades = extract_bbo_and_trades(_build_morning_momentum(direction=1))

    events = detect_intraday_momentum_events(
        bbo, trades, contract="TXFC6", date="2026-03-03", config=_config()
    )

    assert len(events) == 1
    ev = events[0]
    assert ev.regime_type == "T1-D_intraday_session_momentum"
    assert ev.direction == 1
    # Trigger fires at the start of the last window (entry time), not the open.
    assert ev.trigger_time_ns == ns_at(90)
    assert ev.opening_range_high >= 120.0
    assert ev.opening_range_low <= 100.0


def test_detects_short_momentum_from_down_morning():
    bbo, trades = extract_bbo_and_trades(_build_morning_momentum(direction=-1))

    events = detect_intraday_momentum_events(
        bbo, trades, contract="TXFC6", date="2026-03-03", config=_config()
    )

    assert len(events) == 1
    assert events[0].direction == -1


def test_no_event_when_morning_move_below_threshold():
    rows: list[tuple] = []
    prev: tuple[float, float] | None = None

    def push(minute: float, mid: float) -> None:
        nonlocal prev
        ts = ns_at(minute)
        if prev is not None:
            rows.extend(_clear(ts, prev[0], prev[1]))
        rows.extend(_quote(ts, mid - 0.5, mid + 0.5))
        prev = (mid - 0.5, mid + 0.5)

    # Flat morning: |ret_open| ~ 0.1 pt << 10 pt threshold.
    for minute, mid in [
        (1, 100.0), (5, 100.5), (10, 99.5), (15, 100.2), (20, 99.8), (25, 100.1),
        (35, 100.0), (55, 100.0), (75, 100.0),
        (90, 100.0), (95, 100.1), (100, 99.9), (105, 100.0), (110, 100.1), (115, 100.0),
    ]:
        push(minute, mid)
    bbo, trades = extract_bbo_and_trades(np.array(rows, dtype=HFTBT_DTYPE))

    events = detect_intraday_momentum_events(
        bbo, trades, contract="TXFC6", date="2026-03-03", config=_config()
    )

    assert events == []


def test_pair_audit_deducts_round_trip_cost_from_gross_return(tmp_path):
    txf = _build_morning_momentum(direction=1)
    # TMF book: enter long at ask near minute 90, exit on bid path at the 30m
    # horizon (minute 120). Clean +14pt executable move.
    tmf_rows: list[tuple] = []
    prev: tuple[float, float] | None = None
    for minute, mid in [(90, 200.0), (105, 210.0), (119, 215.0)]:
        ts = ns_at(minute)
        if prev is not None:
            tmf_rows.extend(_clear(ts, prev[0], prev[1]))
        tmf_rows.extend(_quote(ts, mid - 0.5, mid + 0.5))
        prev = (mid - 0.5, mid + 0.5)

    txf_path = tmp_path / "TXFC6_2026-03-03_l2.hftbt.npz"
    tmf_path = tmp_path / "TMFC6_2026-03-03_l2.hftbt.npz"
    np.savez(txf_path, data=txf)
    np.savez(tmf_path, data=np.array(tmf_rows, dtype=HFTBT_DTYPE))

    rows = audit_intraday_momentum_pair(
        txf_path=txf_path,
        tmf_path=tmf_path,
        session_minutes=120,
        cost_pts=8.0,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["net_30m_pts"] is not None
    assert row["net_after_cost_30m"] == row["net_30m_pts"] - 8.0
    assert row["cost_pts"] == 8.0


def test_run_audit_writes_traceable_summary_outside_alpha_source(tmp_path):
    raw_dir = tmp_path / "raw"
    out_dir = tmp_path / "validations" / "t1d"
    txf_dir = raw_dir / "txfc6"
    tmf_dir = raw_dir / "tmfc6"
    txf_dir.mkdir(parents=True)
    tmf_dir.mkdir(parents=True)
    np.savez(txf_dir / "TXFC6_2026-03-03_l2.hftbt.npz", data=_build_morning_momentum(direction=1))
    tmf_rows: list[tuple] = []
    prev: tuple[float, float] | None = None
    for minute, mid in [(90, 200.0), (105, 210.0), (119, 215.0)]:
        ts = ns_at(minute)
        if prev is not None:
            tmf_rows.extend(_clear(ts, prev[0], prev[1]))
        tmf_rows.extend(_quote(ts, mid - 0.5, mid + 0.5))
        prev = (mid - 0.5, mid + 0.5)
    np.savez(tmf_dir / "TMFC6_2026-03-03_l2.hftbt.npz", data=np.array(tmf_rows, dtype=HFTBT_DTYPE))

    summary = run_intraday_momentum_audit(
        SimpleNamespace(
            raw_dir=str(raw_dir),
            out_dir=str(out_dir),
            months="C6",
            max_date=None,
            min_date=None,
            max_pairs=None,
            session_tz_offset_hours=8,
            cost_pts=8.0,
            session_minutes=120,
            open_window_minutes=30,
            predict_window_minutes=30,
            min_open_move_pts=10.0,
            oos_start="2026-03-03",
            edge_floor_pts=10.0,
        )
    )

    summary_path = Path(summary["summary_path"])
    assert summary_path.parent == out_dir
    assert "research/alphas" not in summary["summary_path"]
    persisted = json.loads(summary_path.read_text(encoding="utf-8"))
    assert persisted["artifact_scope"] == "validation_summary"
    assert persisted["edge_floor_metric"] == "mean_net_edge_pts_per_trade"
    assert persisted["candidate"] == "t1d_txf_intraday_momentum_tmf"
    assert "out_of_sample" in persisted["splits"]


def test_summarizer_marks_undersized_sample_needs_more_days():
    rows = [
        {
            "contract": "TXFD6->TMFD6",
            "date": "2026-03-26",
            "net_after_cost_30m": 12.0,
            "net_30m_pts": 20.0,
            "stop_structure_breached": False,
        }
    ]
    summary = summarize_intraday_momentum_rows(
        rows, audited_dates=["2026-03-26"], oos_start="2026-03-26"
    )

    assert summary["verdict"] == "NEEDS-MORE-DAYS"
    assert summary["hard_gate"]["events_ok"] is False
    assert summary["hard_gate"]["cross_contract_complete"] is False
    assert summary["edge_floor_cleared"] is True
    assert summary["research_decision"]["status"] == "needs_more_sample"


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
    summary = summarize_intraday_momentum_rows(rows)

    assert summary["verdict"] == "KILL"
    assert summary["hard_gate"]["median_net_positive"] is False
