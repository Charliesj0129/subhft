from __future__ import annotations

from datetime import datetime, timezone

import pytest

from research.experiments.validations.cd_free_qt_cx_taiwan_v0.diagnostic import (
    CisdArm,
    CycleSnapshot,
    Event,
    L2Bar,
    OhlcBar,
    _five_minute_bar_query,
    _l2_bar_query,
    advance_cisd,
    attach_l2_attribution,
    baseline_comparison_summary,
    build_diagnostic_payload,
    build_equal_weight_basket,
    compose_channels,
    coverage_summary,
    cycle_key,
    detect_fvg,
    detect_smt,
    detect_sweep,
    front_contract_for_date,
    prior_date_lead_lag,
    run_event_engine,
)


def _ns(hour: int, minute: int) -> int:
    return int(datetime(2026, 4, 1, hour, minute, tzinfo=timezone.utc).timestamp() * 1_000_000_000)


def _bar(symbol: str, end_ns: int, o: float, h: float, low: float, c: float) -> OhlcBar:
    return OhlcBar(
        symbol=symbol,
        trade_date="2026-04-01",
        end_ns=end_ns,
        open=o,
        high=h,
        low=low,
        close=c,
    )


def _cycle(symbol: str, key: str, o: float, h: float, low: float, c: float) -> CycleSnapshot:
    return CycleSnapshot(
        symbol=symbol,
        layer="90m",
        cycle_key=key,
        start_ns=1,
        end_ns=2,
        open=o,
        high=h,
        low=low,
        close=c,
        high_ts_ns=1,
        low_ts_ns=1,
    )


def test_equal_weight_basket_is_session_normalized_and_fails_closed_without_future_fill():
    t1 = _ns(1, 5)
    t2 = _ns(1, 10)
    rows = {
        "A": [_bar("A", t1, 100, 102, 99, 101), _bar("A", t2, 101, 104, 100, 103)],
        "B": [_bar("B", t1, 200, 202, 198, 200)],
    }

    basket = build_equal_weight_basket(rows, symbols=("A", "B"), min_valid=2, name="electronic")

    assert basket[0].valid is True
    assert basket[0].valid_count == 2
    assert basket[0].open == pytest.approx(1.0)
    assert basket[0].close == pytest.approx((1.01 + 1.0) / 2)
    assert basket[1].valid is False
    assert basket[1].valid_count == 1
    assert basket[1].missing_symbols == ("B",)


@pytest.mark.parametrize(
    ("end_hour", "end_minute", "duration", "expected_index"),
    [
        (1, 5, 10, 0),
        (1, 10, 10, 0),
        (1, 15, 10, 1),
        (2, 30, 90, 0),
        (2, 35, 90, 1),
        (5, 30, 270, 0),
    ],
)
def test_cycle_key_assigns_bar_end_to_completed_interval(
    end_hour: int,
    end_minute: int,
    duration: int,
    expected_index: int,
):
    key = cycle_key(_ns(end_hour, end_minute), duration_minutes=duration, session_open_hour_utc=1)
    assert key.endswith(f":{duration}m:{expected_index}")


def test_sweep_and_smt_preserve_directional_formulas():
    previous = _cycle("TXF", "p", 100, 110, 90, 105)
    current = _cycle("TXF", "c", 105, 112, 95, 108)

    sweep = detect_sweep(current, previous, confirmed_ts_ns=10)

    assert [(event.kind, event.direction) for event in sweep] == [("sweep", -1)]

    prior = {
        "TXF": previous,
        "electronic": _cycle("electronic", "p", 1, 1.10, 0.90, 1.0),
        "financial": _cycle("financial", "p", 1, 1.10, 0.90, 1.0),
    }
    active = {
        "TXF": current,
        "electronic": _cycle("electronic", "c", 1, 1.08, 0.92, 1.0),
        "financial": _cycle("financial", "c", 1, 1.12, 0.88, 1.0),
    }

    smt = detect_smt(active, prior, layer="90m", confirmed_ts_ns=10, cycle_key_value="c")

    assert any(event.direction == -1 and event.metadata["correlated_leg"] == "electronic" for event in smt)
    assert not any(event.direction == -1 and event.metadata["correlated_leg"] == "financial" for event in smt)


def test_fvg_uses_three_completed_cycles_and_cannot_tap_at_creation_time():
    completed = [
        _cycle("TXF", "old", 100, 101, 95, 99),
        _cycle("TXF", "middle", 101, 106, 100, 105),
        _cycle("TXF", "new", 107, 110, 107, 109),
    ]

    fvg = detect_fvg(completed, created_ts_ns=20)

    assert fvg is not None
    assert fvg.direction == 1
    assert fvg.zone_low == 101
    assert fvg.zone_high == 107
    assert fvg.apply(_bar("TXF", 20, 108, 109, 100, 101)) == ()
    tap_events = fvg.apply(_bar("TXF", 21, 108, 109, 105, 106))
    assert [event.kind for event in tap_events] == ["fvg_tap"]


def test_cisd_threshold_is_frozen_and_confirms_only_on_later_close():
    bars = [
        _bar("TXF", 1, 100, 101, 99, 99),
        _bar("TXF", 2, 99, 103, 98, 102),
        _bar("TXF", 3, 101, 105, 100, 104),
    ]
    arm = CisdArm.from_extreme(bars, direction=-1, cycle_key_value="d:90m:0")

    assert arm is not None
    assert arm.threshold == 99
    assert arm.extreme == 105
    assert advance_cisd(arm, _bar("TXF", 3, 104, 105, 98, 98)) == ()

    events = advance_cisd(arm, _bar("TXF", 4, 104, 104, 97, 98))

    assert [(event.kind, event.direction) for event in events] == [("cisd", -1)]
    assert events[0].metadata["threshold"] == 99


def test_channels_require_ordered_predecessors_in_same_cycle():
    cycle = "2026-04-01:90m:1"
    events = [
        Event.make("sweep", -1, "90m", 10, cycle, "TXF"),
        Event.make("ssmt", -1, "30m", 11, cycle, "triad"),
        Event.make("fvg_tap", -1, "90m", 12, cycle, "TXF"),
        Event.make("cisd", -1, "90m", 13, cycle, "TXF"),
        Event.make("cisd", 1, "90m", 9, cycle, "TXF"),
    ]

    channels = compose_channels(events)

    assert [event.kind for event in channels] == [
        "baseline_sweep_cisd",
        "correlated_channel",
        "main_pair_channel",
    ]
    assert all(event.ts_ns == 13 for event in channels)


def test_lead_lag_for_each_date_uses_strictly_prior_dates():
    rows = []
    for day, txf, electronic, financial in [
        ("2026-04-01", [0, 1, 2, 3], [1, 2, 3, 4], [0, 0, 0, 0]),
        ("2026-04-02", [0, 2, 4, 6], [2, 4, 6, 8], [0, 0, 0, 0]),
        ("2026-04-03", [100, -100, 100, -100], [-100, 100, -100, 100], [1, 1, 1, 1]),
    ]:
        for idx in range(4):
            rows.append(
                {
                    "date": day,
                    "index": idx,
                    "txf_return": txf[idx],
                    "electronic_return": electronic[idx],
                    "financial_return": financial[idx],
                }
            )

    estimates = prior_date_lead_lag(rows, min_pairs=3)

    day3 = next(row for row in estimates if row["date"] == "2026-04-03")
    assert day3["leader"] == "electronic"
    assert day3["training_dates"] == 2


def test_l2_attribution_marks_next_bar_refill_as_outcome_only():
    event = Event.make("sweep", -1, "90m", 10, "c", "TXF")
    l2 = {
        10: L2Bar(10, spread_mean=2.0, gap_p95=3.0, depth_mean=100.0, signed_aggressiveness=-0.4),
        20: L2Bar(20, spread_mean=1.0, gap_p95=1.0, depth_mean=150.0, signed_aggressiveness=0.1),
    }

    attributed = attach_l2_attribution([event], l2)

    assert attributed[0]["same_bar"]["signed_aggressiveness"] == -0.4
    assert attributed[0]["next_bar_outcome_only"]["depth_refill_ratio"] == 1.5
    assert attributed[0]["next_bar_outcome_only"]["spread_resiliency_ratio"] == 0.5
    assert attributed[0]["next_bar_outcome_only"]["usable_as_signal"] is False


def test_front_contract_chain_is_frozen_and_missing_days_are_not_backfilled():
    assert front_contract_for_date("2026-02-18") == "TXFB6"
    assert front_contract_for_date("2026-02-19") == "TXFC6"
    assert front_contract_for_date("2026-04-16") == "TXFE6"
    assert front_contract_for_date("2026-05-21") == "TXFF6"


def test_coverage_summary_keeps_zero_valid_dates_visible():
    t1 = _ns(1, 5)
    t2 = _ns(1, 10)
    txf = [
        _bar("TXF", t1, 100, 101, 99, 100),
        OhlcBar("TXF", "2026-04-02", t2 + 86_400_000_000_000, 100, 101, 99, 100),
    ]
    electronic = [
        OhlcBar("electronic", "2026-04-01", t1, 1, 1, 1, 1, valid=True, valid_count=23),
        OhlcBar(
            "electronic",
            "2026-04-02",
            t2 + 86_400_000_000_000,
            1,
            1,
            1,
            1,
            valid=False,
            valid_count=18,
        ),
    ]
    financial = [
        OhlcBar("financial", "2026-04-01", t1, 1, 1, 1, 1, valid=True, valid_count=12),
        OhlcBar(
            "financial",
            "2026-04-02",
            t2 + 86_400_000_000_000,
            1,
            1,
            1,
            1,
            valid=False,
            valid_count=9,
        ),
    ]

    coverage = coverage_summary(txf, electronic, financial, expected_slots_per_day=54)

    assert coverage["dates"]["2026-04-01"]["triad_valid_bars"] == 1
    assert coverage["dates"]["2026-04-02"]["triad_valid_bars"] == 0
    assert coverage["dates"]["2026-04-02"]["date_valid"] is False


def test_event_engine_is_prefix_invariant_for_emitted_events():
    txf: list[OhlcBar] = []
    electronic: list[OhlcBar] = []
    financial: list[OhlcBar] = []
    base = _ns(1, 5)
    for idx in range(40):
        end_ns = base + idx * 5 * 60_000_000_000
        if idx < 18:
            txf_price = 100 + idx * 0.2
            elec_price = 1 + idx * 0.002
        else:
            txf_price = 110 - (idx - 18) * 0.3
            elec_price = 1.02 + (idx - 18) * 0.003
        txf.append(_bar("TXF", end_ns, txf_price, txf_price + 1.0, txf_price - 1.0, txf_price))
        electronic.append(
            OhlcBar(
                "electronic",
                "2026-04-01",
                end_ns,
                elec_price,
                elec_price + 0.005,
                elec_price - 0.005,
                elec_price,
                valid=True,
                valid_count=23,
            )
        )
        financial.append(
            OhlcBar(
                "financial",
                "2026-04-01",
                end_ns,
                1.0,
                1.001,
                0.999,
                1.0,
                valid=True,
                valid_count=12,
            )
        )

    prefix = run_event_engine(txf[:30], electronic[:30], financial[:30])
    full = run_event_engine(txf, electronic, financial)
    cutoff = txf[29].end_ns

    assert prefix
    assert [event.event_id for event in prefix] == [event.event_id for event in full if event.ts_ns <= cutoff]


def test_diagnostic_payload_is_explicitly_no_trade_and_not_paper_ready():
    ts = _ns(1, 5)
    txf = [_bar("TXF", ts, 100, 101, 99, 100)]
    electronic = [OhlcBar("electronic", "2026-04-01", ts, 1, 1.01, 0.99, 1, valid=True, valid_count=23)]
    financial = [OhlcBar("financial", "2026-04-01", ts, 1, 1.01, 0.99, 1, valid=True, valid_count=12)]

    payload = build_diagnostic_payload(
        txf,
        electronic,
        financial,
        l2_by_end_ns={},
        provenance={"source": "unit_test"},
    )

    assert payload["schema"] == "research.cd_free_qt_cx_taiwan_v0.feasibility.v1"
    assert payload["diagnostic_type"] == "backfill_evidence_read_only_no_trade"
    assert payload["trading_metrics_computed"] is False
    assert payload["ready_for_paper"] is False
    assert payload["coverage"]["dates"]["2026-04-01"]["triad_valid_bars"] == 1


def test_clickhouse_queries_convert_datetime_bucket_to_nanoseconds_without_datetime64_error():
    for query in (
        _five_minute_bar_query("2026-04-01", "2026-04-01"),
        _l2_bar_query("2026-04-01", "2026-04-01"),
    ):
        assert "toUnixTimestamp64Nano(toStartOfInterval" not in query
        assert "toUnixTimestamp(toStartOfInterval" in query
        assert "* 1000000000 + 300000000000" in query


def test_baseline_comparison_reports_selectivity_overlap_and_leader_rows():
    cycle = "2026-04-01:90m:0"
    events = [
        Event.make("baseline_sweep_cisd", -1, "90m", 10, cycle, "TXF"),
        Event.make("correlated_channel", -1, "90m", 10, cycle, "triad"),
        Event.make("main_pair_channel", -1, "90m", 11, cycle, "TXF"),
    ]
    lead_lag = [
        {"date": "2026-04-01", "leader": None},
        {"date": "2026-04-02", "leader": "txf"},
    ]

    summary = baseline_comparison_summary(events, lead_lag, aligned_return_rows=100)

    assert summary["sweep_cisd"]["events"] == 1
    assert summary["correlated_channel"]["events"] == 1
    assert summary["main_pair_channel"]["events"] == 1
    assert summary["correlated_overlap_with_sweep_cisd"] == 1
    assert summary["main_pair_overlap_with_sweep_cisd"] == 0
    assert summary["lead_lag"]["dates_with_prior_estimate"] == 1
    assert summary["lead_lag"]["aligned_return_rows"] == 100
