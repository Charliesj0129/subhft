"""Research v2 PR7: the rolled front-month maker series (ADR-004, option A).

Covers the pre-registered roll protocol (loaded, hashed, refused when it states
a rule the code does not implement), delivery dates and the quoted contract,
parity with the live R1 binding, trading-day windows, ``MakerEngine.run_rolled``
and maker Gate C on a ``<ROOT>_frontmonth`` instrument.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import types
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from hft_platform.alpha._gate_c import run_gate_c
from hft_platform.alpha._validation_types import ValidationConfig
from hft_platform.contracts.family_resolver import build_snapshot_from_calendar
from hft_platform.contracts.ref import FamilyCode, FutureRef
from hft_platform.core.market_calendar import taifex_delivery_cutoff
from research.backtest import rolled_series
from research.backtest.cost_models import load_cost_profile
from research.backtest.fill_models import QueueDepletionFill
from research.backtest.maker_engine import LatencyProfile, MakerEngine, TickData
from research.backtest.rolled_series import (
    RollProtocolError,
    SessionCalendar,
    build_schedule,
    delivery_date,
    load_roll_protocol,
    quoted_contract,
    rolled_root,
    trading_day_window,
)

_ROOT = Path(__file__).resolve().parents[3]
_PROTOCOL = _ROOT / "config" / "research" / "protocols" / "front_month_roll_v1.yaml"
_TZ = ZoneInfo("Asia/Taipei")


@pytest.fixture(scope="module")
def calendar() -> SessionCalendar:
    return SessionCalendar("XTAI")


@pytest.fixture(scope="module")
def protocol() -> rolled_series.RollProtocol:
    return load_roll_protocol()


def _ns(day: dt.date, hh: int, mm: int = 0) -> int:
    return int(dt.datetime.combine(day, dt.time(hh, mm), tzinfo=_TZ).timestamp()) * 1_000_000_000


# --- protocol -----------------------------------------------------------------


def test_shipped_roll_protocol_loads_and_is_hashed_over_its_bytes(protocol) -> None:
    assert protocol.protocol_id == "front_month_roll_v1"
    assert protocol.sha256 == hashlib.sha256(_PROTOCOL.read_bytes()).hexdigest()
    assert protocol.window_start == dt.time(14, 0)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema", "research_protocol.v0"),
        ("calendar", "XTAI_weekdays"),
        ("delivery_date", "third_wednesday"),
        ("quoted_contract", "highest_volume"),
        ("roll_day_pnl", "carry_position"),
        ("trading_day", {"night_session": "same_day", "window_start_local": "14:00"}),
        ("trading_day", {"night_session": "next_trading_day", "window_start_local": "15:00"}),
    ],
)
def test_roll_protocol_stating_another_rule_is_refused(tmp_path: Path, key: str, value: object) -> None:
    doc = yaml.safe_load(_PROTOCOL.read_text(encoding="utf-8"))
    doc[key] = value
    path = tmp_path / "p.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(RollProtocolError):
        load_roll_protocol(path)


def test_rolled_root_recognises_only_the_frontmonth_alias() -> None:
    assert rolled_root("TMF_frontmonth") == "TMF"
    assert rolled_root("TMFD6") is None
    with pytest.raises(RollProtocolError, match="root must be"):
        rolled_root("tmf'; DROP_frontmonth")


# --- delivery dates and the quoted contract -------------------------------------


def test_delivery_date_moves_off_a_holiday_wednesday(calendar) -> None:
    # 2026-02-18 falls in the Lunar New Year closure; the next session is 02-23.
    assert delivery_date(2026, 2, calendar) == dt.date(2026, 2, 23)
    assert delivery_date(2026, 3, calendar) == dt.date(2026, 3, 18)


@pytest.mark.parametrize(
    ("day", "code"),
    [
        (dt.date(2026, 3, 17), "TMFC6"),
        (dt.date(2026, 3, 18), "TMFC6"),  # delivery day: still the expiring month
        (dt.date(2026, 3, 19), "TMFD6"),
        (dt.date(2026, 2, 23), "TMFB6"),  # moved delivery day
        (dt.date(2026, 2, 24), "TMFC6"),
        (dt.date(2026, 12, 17), "TMFA7"),  # past December delivery: next January
    ],
)
def test_quoted_contract_is_the_earliest_delivery_on_or_after_the_day(calendar, protocol, day, code) -> None:
    assert quoted_contract("TMF", day, protocol, calendar) == code


@pytest.mark.parametrize(
    "instant",
    [
        (dt.date(2026, 3, 18), 10, 0),  # delivery-day morning: expiring month
        (dt.date(2026, 3, 18), 13, 29),
        (dt.date(2026, 3, 18), 15, 30),  # delivery-day night: next month
        (dt.date(2026, 3, 17), 22, 0),
        (dt.date(2026, 3, 19), 2, 0),
        (dt.date(2026, 2, 23), 15, 5),
    ],
)
def test_quoted_contract_matches_the_live_r1_binding(calendar, protocol, instant) -> None:
    day, hh, mm = instant
    now_ns = _ns(day, hh, mm)
    refs = [FutureRef("TMF", delivery_date(2026, m, calendar)) for m in range(1, 13)]
    live = build_snapshot_from_calendar({"TMF": refs}, today=taifex_delivery_cutoff(now_ns), snapshot_ns=now_ns)
    (bound,) = [ref for family, ref in live.family_map.items() if family.family is FamilyCode.R1]
    # The trading day an event at this instant belongs to: from 14:00 the night session counts toward the next one.
    local = dt.datetime.fromtimestamp(now_ns / 1e9, _TZ)
    after_close = local.time() >= dt.time(14)
    trading_day = local.date()
    if after_close or not calendar.is_session(trading_day):
        trading_day = calendar.next_session(trading_day)
    assert quoted_contract("TMF", trading_day, protocol, calendar) == bound.display()


# --- trading-day windows and the schedule ------------------------------------------


def test_trading_day_window_runs_from_the_previous_session_afternoon(calendar, protocol) -> None:
    # Monday 2026-03-16: the window opens Friday 03-13 14:00, so it holds Friday's night session.
    start, end = trading_day_window(dt.date(2026, 3, 16), protocol, calendar)
    assert (start, end) == (_ns(dt.date(2026, 3, 13), 14), _ns(dt.date(2026, 3, 16), 14))


def test_build_schedule_counts_trading_days_across_contracts(calendar, protocol) -> None:
    days = [dt.date(2026, 3, 18), dt.date(2026, 3, 17), dt.date(2026, 3, 19), dt.date(2026, 3, 18)]
    schedule = build_schedule("TMF_frontmonth", days, protocol=protocol, calendar=calendar)
    assert [(d.trading_day.isoformat(), d.symbol) for d in schedule.days] == [
        ("2026-03-17", "TMFC6"),
        ("2026-03-18", "TMFC6"),
        ("2026-03-19", "TMFD6"),
    ]
    assert schedule.contracts == ("TMFC6", "TMFD6")


def test_build_schedule_refuses_a_day_that_is_not_a_session(calendar, protocol) -> None:
    with pytest.raises(RollProtocolError, match="not a XTAI trading day"):
        build_schedule("TMF_frontmonth", [dt.date(2026, 3, 14)], protocol=protocol, calendar=calendar)


def test_calendar_refuses_a_day_outside_its_range(calendar) -> None:
    with pytest.raises(RollProtocolError, match="outside the XTAI calendar"):
        calendar.is_session(dt.date(2099, 1, 5))


# --- MakerEngine.run_rolled ------------------------------------------------------------


class _Source:
    """ClickHouseSource stand-in: a crossing trade after each quote in every window."""

    _host, _port = "fake", 0

    def __init__(self, dates: list[dt.date] | None = None) -> None:
        self.windows: list[tuple[str, int, int]] = []
        self._dates = dates or [dt.date(2026, 3, 17), dt.date(2026, 3, 18), dt.date(2026, 3, 19)]

    def health_check(self) -> None:
        return None

    def available_local_dates(self, root: str, tz: str) -> list[dt.date]:
        assert (root, tz) == ("TMF", "Asia/Taipei")
        return [*self._dates, dt.date(2026, 3, 21)]  # a Saturday-morning tail is not a trading day

    def load_window(self, symbol: str, start_ns: int, end_ns: int) -> list[TickData]:
        self.windows.append((symbol, start_ns, end_ns))
        t = end_ns - 3_600_000_000_000
        scale = 1_000_000
        quote = TickData(t, 20000 * scale, 20010 * scale, 1, 1, 0, 0, False, scale)
        trade = TickData(t + 1_000_000, 0, 0, 0, 0, 20000 * scale, 5, True, scale)
        return [quote, trade]


class _Strategy:
    def __init__(self) -> None:
        self.contracts: list[str] = []

    def set_active_symbol(self, symbol: str) -> None:
        self.contracts.append(symbol)

    def on_tick(self, tick: TickData) -> list:
        from hft_platform.contracts.maker_actions import Hold

        return [Hold()]

    def on_fill(self, side: str, price: int, mid_price: float) -> None:
        return None


def _engine(source: _Source) -> MakerEngine:
    return MakerEngine(QueueDepletionFill(queue_fraction=0.5), load_cost_profile("TMF_frontmonth"), ck_source=source)


def test_run_rolled_emits_one_dated_row_per_trading_day_on_its_contract(calendar, protocol) -> None:
    source = _Source()
    schedule = rolled_series.schedule_from_source("TMF_frontmonth", source, protocol=protocol, calendar=calendar)
    strategy = _Strategy()
    result = _engine(source).run_rolled(strategy, schedule)
    assert [(r["date"], r["contract"]) for r in result.daily_pnl] == [
        ("2026-03-17", "TMFC6"),
        ("2026-03-18", "TMFC6"),
        ("2026-03-19", "TMFD6"),
    ]
    assert result.maker_scorecard["n_days"] == 3
    assert result.instrument == "TMF_frontmonth"
    assert strategy.contracts == ["TMFC6", "TMFD6"]  # told once per contract, before its first event
    assert [(w[0], w[1], w[2]) for w in source.windows] == [(d.symbol, d.start_ns, d.end_ns) for d in schedule.days]


def test_run_keeps_single_contract_rows_without_a_contract_field() -> None:
    class _DaySource(_Source):
        def available_dates(self, symbol: str) -> list[str]:
            return ["2026-03-17"]

        def load_day(self, symbol: str, date: str) -> list[TickData]:
            return self.load_window(symbol, 0, _ns(dt.date(2026, 3, 17), 14))

    strategy = _Strategy()
    result = _engine(_DaySource()).run(strategy, "TMFC6")
    assert [r["date"] for r in result.daily_pnl] == ["2026-03-17"]
    assert "contract" not in result.daily_pnl[0]
    assert strategy.contracts == []


def test_rolled_cost_profile_is_the_root_profile() -> None:
    rolled, root = load_cost_profile("TMF_frontmonth"), load_cost_profile("TMF")
    assert rolled.instrument == "TMF_frontmonth"
    assert rolled.apply(10.0, 2) == root.apply(10.0, 2)


# --- maker Gate C ------------------------------------------------------------------------


def _gate_c_runtime(source: _Source, rolled_schedule) -> types.SimpleNamespace:
    maker_runtime = types.SimpleNamespace(
        load_cost_profile=load_cost_profile,
        queue_depletion_fill=QueueDepletionFill,
        clickhouse_source=lambda: source,
        latency_profile=LatencyProfile,
        maker_engine=MakerEngine,
        rolled_schedule=rolled_schedule,
    )

    def _scorecard(payload, **_kw):
        return types.SimpleNamespace(to_dict=lambda: dict(payload, signals=None))

    return types.SimpleNamespace(
        hft_native_runner=None,
        ensure_hftbt_npz=None,
        backtest_config=None,
        walk_forward_config=None,
        compute_scorecard=_scorecard,
        load_maker_runtime=lambda: maker_runtime,
    )


def _maker_alpha(instrument: str) -> types.SimpleNamespace:
    manifest = types.SimpleNamespace(
        alpha_id="maker_x",
        strategy_type="maker",
        instrument=instrument,
        latency_profile="r47_maker_shioaji_p95_v2026-04-24_measured",
    )
    return types.SimpleNamespace(manifest=manifest, create_maker_strategy=_Strategy)


def test_maker_gate_c_runs_a_frontmonth_instrument_on_the_rolled_series(tmp_path: Path, protocol) -> None:
    source = _Source()
    runtime = _gate_c_runtime(source, rolled_series.schedule_from_source)
    report, run_id, _, _, meta_path = run_gate_c(
        _maker_alpha("TMF_frontmonth"),
        ValidationConfig(alpha_id="maker_x", data_paths=[]),
        tmp_path,
        [],
        tmp_path,
        runtime=runtime,
    )
    assert run_id
    assert report.details["roll"] == {
        "protocol_id": "front_month_roll_v1",
        "protocol_sha256": protocol.sha256,
        "contracts": ["TMFC6", "TMFD6"],
    }
    assert [r["contract"] for r in report.details["daily_pnl"]] == ["TMFC6", "TMFC6", "TMFD6"]
    assert report.details["maker_scorecard"]["n_days"] == 3
    stamp = json.loads(Path(meta_path).read_text())["run_stamp"]
    assert stamp["protocol_sha256"] == protocol.sha256


def test_maker_gate_c_single_contract_records_no_protocol(tmp_path: Path) -> None:
    class _DaySource(_Source):
        def available_dates(self, symbol: str) -> list[str]:
            return ["2026-03-17"]

        def load_day(self, symbol: str, date: str) -> list[TickData]:
            return self.load_window(symbol, 0, _ns(dt.date(2026, 3, 17), 14))

    def _never(*_a):
        raise AssertionError("a single contract must not build a rolled schedule")

    report, _, _, _, meta_path = run_gate_c(
        _maker_alpha("TMFC6"),
        ValidationConfig(alpha_id="maker_x", data_paths=[]),
        tmp_path,
        [],
        tmp_path,
        runtime=_gate_c_runtime(_DaySource(), _never),
    )
    assert report.details["roll"] is None
    assert json.loads(Path(meta_path).read_text())["run_stamp"]["protocol_sha256"] is None


def _raise(exc: Exception):
    def _build(*_a):
        raise exc

    return _build


@pytest.mark.parametrize(
    ("rolled_schedule", "reason"),
    [
        (None, "rolled_series_unavailable"),
        (_raise(RollProtocolError("calendar unavailable")), "rolled_series_invalid"),
        (_raise(ValueError("No trading days for TMF_frontmonth")), "rolled_series_invalid"),
    ],
)
def test_maker_gate_c_rolled_series_that_cannot_be_built_is_invalid(tmp_path: Path, rolled_schedule, reason) -> None:
    report, run_id, *_ = run_gate_c(
        _maker_alpha("TMF_frontmonth"),
        ValidationConfig(alpha_id="maker_x", data_paths=[]),
        tmp_path,
        [],
        tmp_path,
        runtime=_gate_c_runtime(_Source(), rolled_schedule),
    )
    assert report.passed is False
    assert report.details["verdict"]["verdict"] == "INVALID"
    assert report.details["error"] == reason
    assert run_id == ""


def test_c17_exposes_its_maker_strategy_to_gate_c() -> None:
    from research.alphas.c17_tmf_frontmonth_native_maker.impl import C17Alpha

    alpha = C17Alpha()
    strategy = alpha.create_maker_strategy()
    assert strategy is alpha.maker
    assert callable(strategy.on_tick) and callable(strategy.on_fill) and callable(strategy.set_active_symbol)
    assert alpha.manifest.instrument == "TMF_frontmonth"
