"""T1-C VWAP-trend session-imbalance V0 detector + hard gate.

Exercises the failed-VWAP-reclaim CONTINUATION construction (trade in the trend
direction, not against it), the persistent-side-fraction and failed-reclaim
guards, the VWAP-reclaim stop structure, the 8-pt net-cost deduction, and the
verdict ordering (negative median -> KILL; positive-but-undersized ->
NEEDS-MORE-DAYS, floor NOT relaxed).

The signal is VWAP-relative, so every fixture seeds a heavy early trade to anchor
the cumulative session trade VWAP near 17000 while the BBO mid trends away.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from hft_platform.alpha.strategy_spec import load_spec, validate_spec
from research.t1.regime_viability import (
    VwapTrendConfig,
    _session_start_ns,
    audit_vwap_trend_pair,
    detect_vwap_trend_events,
    extract_bbo_and_trades,
    run_vwap_trend_audit,
    summarize_vwap_trend_rows,
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

TRADE_EVENT = 0x1
BID_DEPTH = np.uint64(0xE0000002)
ASK_DEPTH = np.uint64(0xD0000002)

NS_PER_MINUTE = 60 * 1_000_000_000
SPEC_PATH = Path("research/alphas/t1c_txf_vwaptrend_tmf/spec.yaml")


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


def _session_array(
    date: str,
    points: list[tuple[float, float]],
    trades: list[tuple[float, float, float]] | None = None,
) -> np.ndarray:
    rows: list[tuple] = []
    prev: float | None = None
    for minute, mid in points:
        ts = session_ns(date, minute)
        if prev is not None:
            rows.extend(_clear(ts, prev))
        rows.extend(_quote(ts, mid))
        prev = mid
    for minute, px, qty in trades or []:
        ts = session_ns(date, minute)
        rows.append((TRADE_EVENT, ts, ts, px, qty, 0, 0, 0.0))
    rows.sort(key=lambda r: r[1])
    return np.array(rows, dtype=HFTBT_DTYPE)


def _config(today: str) -> VwapTrendConfig:
    return VwapTrendConfig(
        session_start_ns=_session_start_ns(today),
        session_minutes=300,
        trend_window_minutes=60,
        min_trend_pts=15.0,
        min_side_fraction=0.80,
        reclaim_tolerance_pts=5.0,
        stop_buffer_pts=15.0,
        step_minutes=5,
        cooldown_minutes=60,
    )


# Heavy early trade anchors VWAP ~17000; BBO mid trends above it, pulls back to
# within 5pt of VWAP at minute 40 (failed reclaim), then resumes up to +20.
_TREND_UP = [
    (0, 17000.0),
    (10, 17008.0),
    (20, 17016.0),
    (30, 17020.0),
    (40, 17004.0),
    (50, 17012.0),
    (60, 17020.0),
]
_TREND_DOWN = [
    (0, 17000.0),
    (10, 16992.0),
    (20, 16984.0),
    (30, 16980.0),
    (40, 16996.0),
    (50, 16988.0),
    (60, 16980.0),
]
_VWAP_TRADES_UP = [(0, 17000.0, 1000.0), (30, 17020.0, 1.0), (60, 17020.0, 1.0)]
_VWAP_TRADES_DOWN = [(0, 17000.0, 1000.0), (30, 16980.0, 1.0), (60, 16980.0, 1.0)]


def test_detects_long_continuation_on_vwap_trend_up():
    bbo, trades = extract_bbo_and_trades(_session_array("2026-04-15", _TREND_UP, _VWAP_TRADES_UP))

    events = detect_vwap_trend_events(
        bbo, trades, contract="TXFD6", date="2026-04-15", config=_config("2026-04-15")
    )

    assert len(events) == 1
    ev = events[0]
    assert ev.regime_type == "T1-C_vwap_trend_continuation"
    assert ev.direction == 1  # trade WITH the trend (continuation), not against it
    assert ev.realized_vol_ratio >= 15.0  # signed VWAP displacement
    assert ev.trade_vwap is not None and abs(ev.trade_vwap - 17000.0) < 1.0
    assert ev.trigger_time_ns == session_ns("2026-04-15", 60)
    # Stop band straddles VWAP by the buffer.
    assert ev.opening_range_low < ev.trade_vwap < ev.opening_range_high


def test_detects_short_continuation_on_vwap_trend_down():
    bbo, trades = extract_bbo_and_trades(_session_array("2026-04-15", _TREND_DOWN, _VWAP_TRADES_DOWN))

    events = detect_vwap_trend_events(
        bbo, trades, contract="TXFD6", date="2026-04-15", config=_config("2026-04-15")
    )

    assert len(events) == 1
    assert events[0].direction == -1  # trade with the down-trend
    assert events[0].realized_vol_ratio <= -15.0


def test_no_event_when_displacement_below_threshold():
    flat = [(0, 17000.0), (10, 17002.0), (20, 17004.0), (30, 17005.0), (40, 17003.0), (50, 17004.0), (60, 17005.0)]
    bbo, trades = extract_bbo_and_trades(_session_array("2026-04-15", flat, _VWAP_TRADES_UP))

    events = detect_vwap_trend_events(
        bbo, trades, contract="TXFD6", date="2026-04-15", config=_config("2026-04-15")
    )

    assert events == []  # mid sits <15pt from VWAP -> no imbalance


def test_no_event_when_pullback_crosses_vwap():
    # The dip to 16990 crosses >5pt past VWAP onto the counter-trend side: this is
    # a genuine reclaim, NOT a failed reclaim -> no continuation entry.
    crossed = [(0, 17000.0), (10, 17008.0), (20, 16990.0), (30, 17008.0), (40, 17016.0), (50, 17012.0), (60, 17020.0)]
    bbo, trades = extract_bbo_and_trades(_session_array("2026-04-15", crossed, _VWAP_TRADES_UP))

    events = detect_vwap_trend_events(
        bbo, trades, contract="TXFD6", date="2026-04-15", config=_config("2026-04-15")
    )

    assert events == []


def test_no_event_when_no_pullback_toward_vwap():
    # Price trends up but never returns within 5pt of VWAP: no failed-reclaim setup.
    no_pullback = [(0, 17020.0), (10, 17025.0), (20, 17030.0), (30, 17028.0), (40, 17035.0), (50, 17030.0), (60, 17040.0)]
    bbo, trades = extract_bbo_and_trades(_session_array("2026-04-15", no_pullback, _VWAP_TRADES_UP))

    events = detect_vwap_trend_events(
        bbo, trades, contract="TXFD6", date="2026-04-15", config=_config("2026-04-15")
    )

    assert events == []


def test_pair_audit_deducts_round_trip_cost_from_gross_return(tmp_path):
    txf = tmp_path / "TXFD6_2026-04-15_l2.hftbt.npz"
    tmf = tmp_path / "TMFD6_2026-04-15_l2.hftbt.npz"
    np.savez(txf, data=_session_array("2026-04-15", _TREND_UP, _VWAP_TRADES_UP))
    # Long entry at minute 60; TMF continues up so the long profits on the exit
    # path, and TXF never reclaims VWAP downward (no stop breach).
    np.savez(
        tmf,
        data=_session_array("2026-04-15", [(60, 17020.0), (70, 17030.0), (90, 17040.0)]),
    )

    rows = audit_vwap_trend_pair(txf_path=txf, tmf_path=tmf, cost_pts=8.0)

    assert len(rows) == 1
    row = rows[0]
    assert row["direction"] == 1
    assert row["vwap_reclaim_failed_or_passed"] == "failed"
    assert row["net_30m_pts"] is not None
    assert row["net_after_cost_30m"] == row["net_30m_pts"] - 8.0
    assert row["cost_pts"] == 8.0
    assert row["stop_structure_breached"] is False


def test_run_audit_writes_summary(tmp_path):
    raw_dir = tmp_path / "raw"
    out_dir = tmp_path / "validations" / "t1c"
    txf_dir = raw_dir / "txfd6"
    tmf_dir = raw_dir / "tmfd6"
    txf_dir.mkdir(parents=True)
    tmf_dir.mkdir(parents=True)
    np.savez(txf_dir / "TXFD6_2026-04-15_l2.hftbt.npz", data=_session_array("2026-04-15", _TREND_UP, _VWAP_TRADES_UP))
    np.savez(
        tmf_dir / "TMFD6_2026-04-15_l2.hftbt.npz",
        data=_session_array("2026-04-15", [(60, 17020.0), (70, 17030.0), (90, 17040.0)]),
    )

    summary = run_vwap_trend_audit(
        SimpleNamespace(
            raw_dir=str(raw_dir),
            out_dir=str(out_dir),
            months="D6",
            max_date=None,
            min_date=None,
            max_pairs=None,
            session_tz_offset_hours=8,
            cost_pts=8.0,
            session_minutes=300,
            trend_window_minutes=60,
            min_trend_pts=15.0,
            min_side_fraction=0.80,
            reclaim_tolerance_pts=5.0,
            stop_buffer_pts=15.0,
            step_minutes=5,
            cooldown_minutes=60,
            oos_start=None,
            edge_floor_pts=10.0,
        )
    )

    persisted = json.loads(Path(summary["summary_path"]).read_text(encoding="utf-8"))
    assert persisted["candidate"] == "t1c_txf_vwaptrend_tmf"
    assert persisted["artifact_scope"] == "validation_summary"
    assert persisted["definition"]["trend_window_minutes"] == 60
    assert persisted["splits"]["full"]["events"] == 1


def test_summarizer_kills_on_negative_median():
    rows = [
        {"contract": "TXFD6->TMFD6", "date": "2026-04-15", "net_after_cost_30m": -12.0, "net_30m_pts": -4.0, "stop_structure_breached": True},
        {"contract": "TXFD6->TMFD6", "date": "2026-04-16", "net_after_cost_30m": -8.0, "net_30m_pts": 0.0, "stop_structure_breached": True},
    ]
    summary = summarize_vwap_trend_rows(rows)

    assert summary["verdict"] == "KILL"
    assert summary["research_decision"]["status"] == "failed"


def test_summarizer_needs_more_days_on_small_positive_sample():
    rows = [
        {"contract": "TXFC6->TMFC6", "date": "2026-03-18", "net_after_cost_30m": 22.0, "net_30m_pts": 30.0, "stop_structure_breached": False},
        {"contract": "TXFD6->TMFD6", "date": "2026-04-15", "net_after_cost_30m": 18.0, "net_30m_pts": 26.0, "stop_structure_breached": False},
    ]
    summary = summarize_vwap_trend_rows(rows)

    assert summary["verdict"] == "NEEDS-MORE-DAYS"
    assert summary["hard_gate"]["median_net_positive"] is True
    assert summary["hard_gate"]["events_ok"] is False
    assert summary["hard_gate"]["cross_contract_complete"] is False
    assert summary["research_decision"]["status"] == "needs_more_sample"


def test_t1c_candidate_has_governed_fixed_spec():
    spec = load_spec(SPEC_PATH)

    assert validate_spec(spec) == []
    assert spec["strategy_name"] == "t1c_txf_vwaptrend_tmf"
    assert spec["validation_plan"]["net_edge_floor_pts"] >= 10.0
