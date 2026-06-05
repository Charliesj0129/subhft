"""T1-F expiration V-reversal V0 detector + settlement-day pairing + hard gate.

Exercises the settlement-day-only thrust-fade construction, the FADE direction,
the 8-pt net-cost deduction, the settlement-date resolver, and the
structural-sample NEEDS-MORE-DAYS verdict (the once-per-month settlement signal
cannot reach the >=20-day / >=80-event floor in the paired span -- the floor is
NOT relaxed).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from hft_platform.alpha.strategy_spec import load_spec, validate_spec
from research.t1.regime_viability import (
    ExpirationVReversalConfig,
    _settlement_date_for_month_code,
    _settlement_day_pairs,
    audit_expiration_v_reversal_pair,
    detect_expiration_v_reversal_events,
    extract_bbo_and_trades,
    run_expiration_v_reversal_audit,
    summarize_expiration_v_reversal_rows,
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
SPEC_PATH = Path("research/alphas/t1f_txf_expiration_vreversal_tmf/spec.yaml")


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


def _config(today: str) -> ExpirationVReversalConfig:
    from research.t1.regime_viability import _session_start_ns

    return ExpirationVReversalConfig(
        session_start_ns=_session_start_ns(today),
        session_minutes=285,
        thrust_window_minutes=90,
        min_thrust_pts=20.0,
        stop_buffer_pts=15.0,
    )


# Settlement day opens 17000, thrusts +40 over the first 90 min, then partially
# reverts (the V) -> fade short.
_THRUST_UP = [(0, 17000.0), (30, 17015.0), (60, 17030.0), (90, 17040.0), (120, 17030.0)]
# Settlement day opens 17000, thrusts -40 over the first 90 min -> fade long.
_THRUST_DOWN = [(0, 17000.0), (30, 16985.0), (60, 16970.0), (90, 16960.0), (120, 16970.0)]


def test_settlement_date_resolver_maps_month_code_to_third_wednesday():
    # 3rd Wednesday of each 2026 delivery month.
    assert _settlement_date_for_month_code("B6") == "2026-02-18"
    assert _settlement_date_for_month_code("C6") == "2026-03-18"
    assert _settlement_date_for_month_code("D6") == "2026-04-15"
    assert _settlement_date_for_month_code("E6") == "2026-05-20"
    # Unparseable codes return None instead of guessing.
    assert _settlement_date_for_month_code("ZZ") is None
    assert _settlement_date_for_month_code("D") is None


def test_detects_short_fade_on_thrust_up():
    today_bbo, today_trades = extract_bbo_and_trades(_session_array("2026-04-15", _THRUST_UP))

    events = detect_expiration_v_reversal_events(
        today_bbo, today_trades, contract="TXFD6", date="2026-04-15", config=_config("2026-04-15")
    )

    assert len(events) == 1
    ev = events[0]
    assert ev.regime_type == "T1-F_expiration_v_reversal"
    assert ev.direction == -1  # fade the up-thrust
    assert ev.realized_vol_ratio > 20.0  # signed thrust carried in reused field
    assert ev.trigger_time_ns == session_ns("2026-04-15", 90)


def test_detects_long_fade_on_thrust_down():
    today_bbo, today_trades = extract_bbo_and_trades(_session_array("2026-04-15", _THRUST_DOWN))

    events = detect_expiration_v_reversal_events(
        today_bbo, today_trades, contract="TXFD6", date="2026-04-15", config=_config("2026-04-15")
    )

    assert len(events) == 1
    assert events[0].direction == 1  # fade the down-thrust
    assert events[0].realized_vol_ratio < -20.0


def test_no_event_when_thrust_below_threshold():
    # Open->90min displacement only ~6pt, below the 20pt threshold.
    flat = [(0, 17000.0), (30, 17003.0), (60, 17005.0), (90, 17006.0), (120, 17004.0)]
    today_bbo, today_trades = extract_bbo_and_trades(_session_array("2026-04-15", flat))

    events = detect_expiration_v_reversal_events(
        today_bbo, today_trades, contract="TXFD6", date="2026-04-15", config=_config("2026-04-15")
    )

    assert events == []


def test_pair_audit_deducts_round_trip_cost_from_gross_return(tmp_path):
    today_txf = tmp_path / "TXFD6_2026-04-15_l2.hftbt.npz"
    today_tmf = tmp_path / "TMFD6_2026-04-15_l2.hftbt.npz"
    np.savez(today_txf, data=_session_array("2026-04-15", _THRUST_UP))
    # Up-thrust -> short. TMF reverts down after entry so a short profits on the
    # executable exit path; TXF never extends past the thrust-high stop (no breach).
    np.savez(
        today_tmf,
        data=_session_array(
            "2026-04-15", [(90, 17040.0), (95, 17035.0), (105, 17028.0), (120, 17020.0)]
        ),
    )

    rows = audit_expiration_v_reversal_pair(
        settlement_txf_path=today_txf,
        settlement_tmf_path=today_tmf,
        cost_pts=8.0,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["direction"] == -1
    assert row["settlement_date"] == "2026-04-15"
    assert row["net_30m_pts"] is not None
    assert row["net_after_cost_30m"] == row["net_30m_pts"] - 8.0
    assert row["cost_pts"] == 8.0
    assert row["stop_structure_breached"] is False


def test_settlement_day_pairs_only_yields_the_settlement_day(tmp_path):
    raw_dir = tmp_path / "raw"
    txf_dir = raw_dir / "txfd6"
    tmf_dir = raw_dir / "tmfd6"
    txf_dir.mkdir(parents=True)
    tmf_dir.mkdir(parents=True)
    # Settlement day (2026-04-15) AND a non-settlement day (2026-04-14) present.
    for d in ("2026-04-14", "2026-04-15"):
        np.savez(txf_dir / f"TXFD6_{d}_l2.hftbt.npz", data=_session_array(d, _THRUST_UP))
        np.savez(tmf_dir / f"TMFD6_{d}_l2.hftbt.npz", data=_session_array(d, _THRUST_UP))

    pairs = _settlement_day_pairs(raw_dir, ["D6"])

    assert len(pairs) == 1
    assert "2026-04-15" in pairs[0][0].name  # only the 3rd-Wednesday settlement day


def test_run_audit_writes_settlement_only_summary(tmp_path):
    raw_dir = tmp_path / "raw"
    out_dir = tmp_path / "validations" / "t1f"
    txf_dir = raw_dir / "txfd6"
    tmf_dir = raw_dir / "tmfd6"
    txf_dir.mkdir(parents=True)
    tmf_dir.mkdir(parents=True)
    np.savez(txf_dir / "TXFD6_2026-04-15_l2.hftbt.npz", data=_session_array("2026-04-15", _THRUST_UP))
    np.savez(
        tmf_dir / "TMFD6_2026-04-15_l2.hftbt.npz",
        data=_session_array("2026-04-15", [(90, 17040.0), (95, 17035.0), (105, 17028.0), (120, 17020.0)]),
    )

    summary = run_expiration_v_reversal_audit(
        SimpleNamespace(
            raw_dir=str(raw_dir),
            out_dir=str(out_dir),
            months="D6",
            max_date=None,
            min_date=None,
            max_pairs=None,
            session_tz_offset_hours=8,
            cost_pts=8.0,
            thrust_window_minutes=90,
            min_thrust_pts=20.0,
            stop_buffer_pts=15.0,
            oos_start=None,
            edge_floor_pts=10.0,
        )
    )

    persisted = json.loads(Path(summary["summary_path"]).read_text(encoding="utf-8"))
    assert persisted["candidate"] == "t1f_txf_expiration_vreversal_tmf"
    assert persisted["artifact_scope"] == "validation_summary"
    assert persisted["definition"]["settlement_day_only"] is True
    assert persisted["splits"]["full"]["events"] == 1


def test_summarizer_needs_more_days_on_sparse_settlements():
    # Two positive settlement events (the fade was not refuted) but only 2 days /
    # 2 contracts: structurally below the >=80-event / >=20-day / 4-contract floor.
    rows = [
        {
            "contract": "TXFC6->TMFC6",
            "date": "2026-03-18",
            "net_after_cost_30m": 65.0,
            "net_30m_pts": 73.0,
            "stop_structure_breached": False,
        },
        {
            "contract": "TXFD6->TMFD6",
            "date": "2026-04-15",
            "net_after_cost_30m": 25.0,
            "net_30m_pts": 33.0,
            "stop_structure_breached": False,
        },
    ]
    summary = summarize_expiration_v_reversal_rows(rows)

    assert summary["verdict"] == "NEEDS-MORE-DAYS"
    assert summary["hard_gate"]["median_net_positive"] is True  # not refuted...
    assert summary["hard_gate"]["events_ok"] is False  # ...but sample-blocked
    assert summary["hard_gate"]["trading_days_ok"] is False
    assert summary["hard_gate"]["cross_contract_complete"] is False
    assert summary["research_decision"]["status"] == "needs_more_sample"


def test_t1f_candidate_has_governed_fixed_spec():
    spec = load_spec(SPEC_PATH)

    assert validate_spec(spec) == []
    assert spec["strategy_name"] == "t1f_txf_expiration_vreversal_tmf"
    assert spec["validation_plan"]["net_edge_floor_pts"] >= 10.0
