"""T1-B volatility-compression -> directional-expansion V0 detector + hard gate.

Mirrors the synthetic-event style of ``test_t1_regime_viability`` but exercises
the compression coil -> break mechanism, the 8-pt net-cost deduction, and the
hard-gate / IS-OOS summarizer.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from hft_platform.alpha.strategy_spec import load_spec, validate_spec
from research.t1.regime_viability import (
    VolCompressionConfig,
    audit_vol_compression_pair,
    detect_vol_compression_events,
    extract_bbo_and_trades,
    run_vol_compression_audit,
    summarize_vol_compression_rows,
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
SPEC_PATH = Path("research/alphas/t1b_txf_volcompress_tmf/spec.yaml")


def ns_at(minute: float) -> int:
    base = datetime(2026, 3, 3, 0, 45, 0, tzinfo=timezone.utc)
    return int(base.timestamp() * 1_000_000_000) + int(minute * NS_PER_MINUTE)


def _quote(ts: int, bid: float, ask: float, qty: float = 1.0) -> list[tuple]:
    return [
        (BID_DEPTH, ts, ts, bid, qty, 0, 0, 0.0),
        (ASK_DEPTH, ts, ts, ask, qty, 0, 0, 0.0),
    ]


def _clear(ts: int, bid: float, ask: float) -> list[tuple]:
    # Zero-qty rows retire the previous top-of-book level (per extract logic).
    return [
        (BID_DEPTH, ts, ts, bid, 0.0, 0, 0, 0.0),
        (ASK_DEPTH, ts, ts, ask, 0.0, 0, 0, 0.0),
    ]


def _trade(ts: int, px: float, qty: float = 2.0) -> tuple:
    return (ASK_TRADE, ts, ts, px, qty, 0, 0, 0.0)


def _build_compression_then_break(*, direction: int) -> np.ndarray:
    """Wide baseline 0-30m, tight coil 30-60m, then an 8-pt break after 60m."""
    rows: list[tuple] = []
    prev: tuple[float, float] | None = None

    def push(minute: float, bid: float, ask: float, *, trade: bool = False) -> None:
        nonlocal prev
        ts = ns_at(minute)
        if prev is not None:
            rows.extend(_clear(ts, prev[0], prev[1]))
        rows.extend(_quote(ts, bid, ask))
        if trade:
            rows.append(_trade(ts + 1_000_000_000, ask if direction > 0 else bid))
        prev = (bid, ask)

    # Baseline window (0-30m): wide swings -> high realized vol.
    for minute, mid in [(1, 100.0), (5, 130.0), (9, 95.0), (14, 135.0), (18, 90.0), (22, 120.0), (27, 98.0)]:
        push(minute, mid - 0.5, mid + 0.5)
    # Compression window (30-60m): tiny swings around 110 -> low realized vol.
    for minute, mid in [(31, 110.0), (35, 110.5), (40, 109.5), (45, 110.2), (50, 109.8), (55, 110.1)]:
        push(minute, mid - 0.5, mid + 0.5)
    # Break window (60-90m): decisive directional move beyond coil range +/- 8pt.
    if direction > 0:
        breaks = [(62, 121.0), (70, 130.0), (78, 134.0), (85, 138.0)]
    else:
        breaks = [(62, 100.0), (70, 92.0), (78, 88.0), (85, 84.0)]
    for minute, mid in breaks:
        push(minute, mid - 0.5, mid + 0.5, trade=True)
    return np.array(rows, dtype=HFTBT_DTYPE)


def _config() -> VolCompressionConfig:
    return VolCompressionConfig(
        session_start_ns=ns_at(0),
        session_minutes=120,
        baseline_minutes=30,
        compression_minutes=30,
        break_window_minutes=30,
        step_minutes=5,
        max_compression_ratio=0.70,
        min_break_points=8.0,
        cooldown_minutes=60,
    )


def test_t1b_candidate_has_governed_fixed_spec():
    spec = load_spec(SPEC_PATH)

    errors = validate_spec(spec)

    assert errors == []
    assert spec["strategy_name"] == "t1b_txf_volcompress_tmf"
    assert spec["validation_plan"]["net_edge_floor_pts"] >= 10.0
    assert "edge_per_round_trip" in spec["validation_plan"]["required_gates"]
    assert "replay_parity" in spec["validation_plan"]["required_gates"]


def test_detects_long_break_out_of_compression_coil():
    bbo, trades = extract_bbo_and_trades(_build_compression_then_break(direction=1))

    events = detect_vol_compression_events(
        bbo, trades, contract="TXFC6", date="2026-03-03", config=_config()
    )

    assert len(events) >= 1
    ev = events[0]
    assert ev.regime_type == "T1-B_vol_compression_expansion"
    assert ev.direction == 1
    # Compression range high carried in the reused opening_range_high field.
    assert ev.opening_range_high <= 111.0
    assert ev.realized_vol_ratio <= 0.70  # genuine compression


def test_detects_short_break_out_of_compression_coil():
    bbo, trades = extract_bbo_and_trades(_build_compression_then_break(direction=-1))

    events = detect_vol_compression_events(
        bbo, trades, contract="TXFC6", date="2026-03-03", config=_config()
    )

    assert len(events) >= 1
    assert events[0].direction == -1


def test_no_event_when_volatility_does_not_compress():
    # Replace the coil window with continued wide swings -> ratio > 0.70.
    rows: list[tuple] = []
    prev: tuple[float, float] | None = None

    def push(minute: float, mid: float) -> None:
        nonlocal prev
        ts = ns_at(minute)
        if prev is not None:
            rows.extend(_clear(ts, prev[0], prev[1]))
        rows.extend(_quote(ts, mid - 0.5, mid + 0.5))
        prev = (mid - 0.5, mid + 0.5)

    for minute, mid in [
        (1, 100.0), (6, 130.0), (11, 95.0), (16, 135.0), (21, 90.0), (26, 120.0),
        (31, 100.0), (37, 132.0), (43, 96.0), (49, 134.0), (55, 92.0),  # still wide
        (62, 150.0), (70, 160.0), (85, 170.0),
    ]:
        push(minute, mid)
    bbo, trades = extract_bbo_and_trades(np.array(rows, dtype=HFTBT_DTYPE))

    events = detect_vol_compression_events(
        bbo, trades, contract="TXFC6", date="2026-03-03", config=_config()
    )

    assert events == []


def test_pair_audit_deducts_round_trip_cost_from_gross_return(tmp_path):
    txf = _build_compression_then_break(direction=1)
    # TMF book: enter long at ask, exit on bid path; build a clean +N pt move.
    tmf_rows: list[tuple] = []
    prev: tuple[float, float] | None = None
    for minute, mid in [(62, 110.0), (77, 130.0), (92, 132.0)]:
        ts = ns_at(minute)
        if prev is not None:
            tmf_rows.extend(_clear(ts, prev[0], prev[1]))
        tmf_rows.extend(_quote(ts, mid - 0.5, mid + 0.5))
        prev = (mid - 0.5, mid + 0.5)

    txf_path = tmp_path / "TXFC6_2026-03-03_l2.hftbt.npz"
    tmf_path = tmp_path / "TMFC6_2026-03-03_l2.hftbt.npz"
    np.savez(txf_path, data=txf)
    np.savez(tmf_path, data=np.array(tmf_rows, dtype=HFTBT_DTYPE))

    rows = audit_vol_compression_pair(
        txf_path=txf_path,
        tmf_path=tmf_path,
        session_minutes=120,
        cost_pts=8.0,
    )

    assert len(rows) >= 1
    row = rows[0]
    assert row["net_30m_pts"] is not None
    assert row["net_after_cost_30m"] == row["net_30m_pts"] - 8.0
    assert row["cost_pts"] == 8.0


def test_run_audit_writes_traceable_summary_outside_alpha_source(tmp_path):
    raw_dir = tmp_path / "raw"
    out_dir = tmp_path / "validations" / "t1b"
    txf_dir = raw_dir / "txfc6"
    tmf_dir = raw_dir / "tmfc6"
    txf_dir.mkdir(parents=True)
    tmf_dir.mkdir(parents=True)
    txf_path = txf_dir / "TXFC6_2026-03-03_l2.hftbt.npz"
    tmf_path = tmf_dir / "TMFC6_2026-03-03_l2.hftbt.npz"
    np.savez(txf_path, data=_build_compression_then_break(direction=1))
    tmf_rows: list[tuple] = []
    prev: tuple[float, float] | None = None
    for minute, mid in [(62, 110.0), (77, 130.0), (92, 132.0)]:
        ts = ns_at(minute)
        if prev is not None:
            tmf_rows.extend(_clear(ts, prev[0], prev[1]))
        tmf_rows.extend(_quote(ts, mid - 0.5, mid + 0.5))
        prev = (mid - 0.5, mid + 0.5)
    np.savez(tmf_path, data=np.array(tmf_rows, dtype=HFTBT_DTYPE))

    summary = run_vol_compression_audit(
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
            baseline_minutes=30,
            compression_minutes=30,
            break_window_minutes=30,
            step_minutes=5,
            max_compression_ratio=0.70,
            min_break_points=8.0,
            cooldown_minutes=60,
            oos_start="2026-03-03",
            edge_floor_pts=10.0,
        )
    )

    summary_path = Path(summary["summary_path"])
    assert summary_path.parent == out_dir
    assert "research/alphas" not in summary["summary_path"]
    persisted = json.loads(summary_path.read_text(encoding="utf-8"))
    assert persisted["summary_path"] == str(summary_path)
    assert persisted["artifact_scope"] == "validation_summary"
    assert persisted["edge_floor_metric"] == "mean_net_edge_pts_per_trade"
    assert persisted["splits"]["full"]["mean_net_edge_pts_per_trade"] is not None
    assert persisted["research_decision"]["status"] in {"needs_more_sample", "blocked_by_audit"}
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
    summary = summarize_vol_compression_rows(rows, audited_dates=["2026-03-26"], oos_start="2026-03-26")

    assert summary["verdict"] == "NEEDS-MORE-DAYS"
    assert summary["hard_gate"]["events_ok"] is False
    assert summary["hard_gate"]["cross_contract_complete"] is False
    # Edge floor IS reported even on an undersized sample.
    assert summary["edge_floor_cleared"] is True
    assert summary["research_decision"]["status"] == "needs_more_sample"
    assert "min_sample_size" in summary["research_decision"]["evidence"]
    assert "out_of_sample" in summary["splits"]


def test_edge_floor_uses_mean_net_edge_per_round_trip_not_median():
    rows = [
        {
            "contract": f"TXF{contract}6->TMF{contract}6",
            "date": f"2026-03-{idx:02d}",
            "net_after_cost_30m": net,
            "net_30m_pts": net + 8.0,
            "stop_structure_breached": False,
        }
        for idx, (contract, net) in enumerate(
            [
                ("B", -40.0),
                ("C", 12.0),
                ("D", 12.0),
            ],
            start=1,
        )
    ]

    summary = summarize_vol_compression_rows(rows)

    full = summary["splits"]["full"]
    assert full["median_net_after_cost_30m"] == 12.0
    assert full["mean_net_edge_pts_per_trade"] == -16.0 / 3.0
    assert summary["edge_floor_metric"] == "mean_net_edge_pts_per_trade"
    assert summary["edge_floor_cleared"] is False


def test_summarizer_blocks_v0_proceeding_sample_by_audit_until_gate_c_evidence_exists():
    rows = [
        {
            "contract": f"TXF{contract}6->TMF{contract}6",
            "date": f"2026-03-{idx:02d}",
            "net_after_cost_30m": net,
            "net_30m_pts": net + 8.0,
            "stop_structure_breached": False,
        }
        for idx, (contract, net) in enumerate(
            [
                ("B", 12.0),
                ("C", 13.0),
                ("D", 14.0),
            ],
            start=1,
        )
    ]

    summary = summarize_vol_compression_rows(
        rows,
        audited_dates=["2026-03-01", "2026-03-02", "2026-03-03"],
        min_events=3,
        min_trading_days=3,
        required_contracts=("TXFB6", "TXFC6", "TXFD6"),
    )

    assert summary["verdict"] == "PROCEED"
    assert summary["edge_floor_cleared"] is True
    assert summary["research_decision"]["status"] == "blocked_by_audit"
    assert "v0_latency_profile_deferred" in summary["research_decision"]["evidence"]
    assert "no_replay_paper_live_parity_evidence" in summary["research_decision"]["evidence"]


def test_summarizer_blocks_when_drawdown_exceeds_monthly_stability_gate():
    nets_by_day = [
        ("TXFB6->TMFB6", "2026-01-02", 40.0),
        ("TXFC6->TMFC6", "2026-01-03", -15.0),
        ("TXFD6->TMFD6", "2026-02-02", 10.0),
        ("TXFE6->TMFE6", "2026-02-03", -30.0),
        ("TXFB6->TMFB6", "2026-03-02", 35.0),
    ]
    rows = [
        {
            "contract": contract,
            "date": date,
            "net_after_cost_30m": net,
            "net_30m_pts": net + 8.0,
            "stop_structure_breached": False,
        }
        for contract, date, net in nets_by_day
    ]

    summary = summarize_vol_compression_rows(
        rows,
        audited_dates=[date for _, date, _ in nets_by_day],
        min_events=5,
        min_trading_days=5,
    )

    full = summary["splits"]["full"]
    assert full["monthly_net_pnl"] == {
        "2026-01": 25.0,
        "2026-02": -20.0,
        "2026-03": 35.0,
    }
    assert full["max_drawdown_net_pts"] == 35.0
    assert full["average_monthly_net_pnl"] == 40.0 / 3.0
    assert full["median_monthly_net_pnl"] == 25.0
    assert full["worst_month_net_pnl"] == -20.0
    assert full["drawdown_within_2x_average_monthly_net_pnl"] is False
    assert summary["hard_gate"]["drawdown_within_2x_average_monthly_net_pnl"] is False
    assert summary["research_decision"]["status"] == "blocked_by_risk"
    assert "max_drawdown_vs_average_monthly_net_pnl" in summary["research_decision"]["evidence"]


def test_summarizer_marks_drawdown_gate_false_when_average_monthly_net_is_non_positive():
    rows = [
        {
            "contract": f"TXF{contract}6->TMF{contract}6",
            "date": date,
            "net_after_cost_30m": net,
            "net_30m_pts": net + 8.0,
            "stop_structure_breached": False,
        }
        for contract, date, net in [
            ("B", "2026-01-02", -20.0),
            ("C", "2026-02-02", 5.0),
            ("D", "2026-03-02", -10.0),
            ("E", "2026-04-02", 5.0),
        ]
    ]

    summary = summarize_vol_compression_rows(rows)

    full = summary["splits"]["full"]
    assert full["average_monthly_net_pnl"] == -5.0
    assert full["drawdown_within_2x_average_monthly_net_pnl"] is False
    assert summary["hard_gate"]["drawdown_within_2x_average_monthly_net_pnl"] is False


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
    summary = summarize_vol_compression_rows(rows)

    assert summary["verdict"] == "KILL"
    assert summary["hard_gate"]["median_net_positive"] is False
