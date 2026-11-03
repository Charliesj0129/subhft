"""Contract-roll drill: fast-forward across two real settlements, every CI run.

The roll code was fixed nine times between 2026-04 and 2026-11, and each fix
was proven only at the next third Wednesday, in production. This drill moves a
simulated clock across the 2026-11-18 (TMFK6/TXFK6/MXFK6) and 2026-12-16
(L6) deliveries in 15-minute steps and checks every consumer of "the front
month" at every step, so a regression shows up on the day it is written.

Input is the broker's own data, not a hand-made list:
``tests/fixtures/contracts/shioaji_179_index_futures_2026-11-03.json`` holds
the TMF/TXF/MXF futures rows of the production shioaji 1.7.9 contract cache
verbatim, including the ``R1``/``R2`` continuous rows that share their month's
delivery date. The cache is deliberately *not* refreshed during the drill: in
production it is rewritten on login, so the roll must work off the listing it
read before the settlement.

Consumers checked at every step (all real code, no doubles except the SDK
object shape and the session clock):

- the subscription universe (``QuoteConnectionPool.roll_universe``, run only
  when the engine's own clock would run it: outside a session);
- the strategy binding (``ShioajiFamilyPopulator.roll_if_due``, the 60 s
  family clock that rebinds R47's ``TMF:R1``);
- the session gate (``TrackGate`` from the real ``session_governor.yaml``);
- startup recovery of a checkpointed position in the settling contract;
- the order-path expiry window (``risk/expiry.py``).

Not covered: TXO option windows (the fixture holds no option rows), and a
broker listing that changes during the drill.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import yaml

from hft_platform.config import _symbols_types as symbols_types
from hft_platform.contracts.family_resolver import ContractFamilyResolver
from hft_platform.contracts.ref import ContractFamily, FamilyCode, Product
from hft_platform.core import timebase
from hft_platform.core.market_calendar import get_calendar, taifex_delivery_cutoff
from hft_platform.execution.positions import PositionStore
from hft_platform.execution.startup_recon import StartupPositionVerifier
from hft_platform.feed_adapter.shioaji.family_populator import ShioajiFamilyPopulator
from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool
from hft_platform.ops.session_governor import SessionGovernor, SessionPhase
from hft_platform.risk.expiry import DEFAULT_EXPIRY_CUTOFF_MINUTE, expiry_window_ns

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/contracts/shioaji_179_index_futures_2026-11-03.json"
SESSION_GOVERNOR_YAML = REPO / "config/base/session_governor.yaml"
TAIPEI = ZoneInfo("Asia/Taipei")
ROOTS = ("TMF", "TXF", "MXF")
STEP = dt.timedelta(minutes=15)
# The pre-open lead the pool's reconnect window uses (HFT_FACADE_RECONNECT_PRE_OPEN_LEAD_S default).
PRE_OPEN_LEAD = dt.timedelta(seconds=900)

# The futures lines of config/symbols.list, verbatim. The test below fails if
# they drift, so the drill always rolls the list production rolls.
FUTURES_LIST_LINES = [
    "TXF@front exchange=FUT tags=futures|front_month|txf",
    "TXF@next exchange=FUT tags=futures|next_month|txf",
    "TXF@far exchange=FUT tags=futures|far_month|txf",
    "MXF@front exchange=FUT tags=futures|front_month|mxf",
    "MXF@next exchange=FUT tags=futures|next_month|mxf",
    "TMF@front exchange=FUT tags=futures|front_month|tmf",
    "TMF@next exchange=FUT tags=futures|next_month|tmf",
]


def _rows() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["contracts"]


def _delivery(row: dict[str, Any]) -> dt.date:
    return dt.date.fromisoformat(str(row["delivery_date"]).replace("/", "-"))


def _is_alias(code: str) -> bool:
    return code[-2:] in {"R1", "R2"}


def _front_by_broker(root: str, now_ns: int) -> str:
    """The drill's oracle: earliest listed month still deliverable at ``now_ns``."""
    cutoff = taifex_delivery_cutoff(now_ns)
    live = [r for r in _rows() if r["root"] == root and not _is_alias(r["code"]) and _delivery(r) >= cutoff]
    return min(live, key=lambda r: (_delivery(r), r["code"]))["code"]


def _settled(code: str, now_ns: int) -> bool:
    row = next(r for r in _rows() if r["code"] == code)
    return _delivery(row) < taifex_delivery_cutoff(now_ns)


def _ns(local: dt.datetime) -> int:
    return int(local.timestamp() * 1_000_000_000)


def _steps(start: dt.datetime, end: dt.datetime) -> list[dt.datetime]:
    out = []
    t = start
    while t <= end:
        out.append(t)
        t += STEP
    return out


def _session_open_or_lead(local: dt.datetime) -> bool:
    """``QuoteConnectionPool.reconnect_allowed`` evaluated at the simulated time."""
    calendar = get_calendar()
    return bool(
        calendar.is_trading_hours(local, product_type="future")
        or calendar.is_trading_hours(local + PRE_OPEN_LEAD, product_type="future")
    )


class _Futures:
    """``api.Contracts.Futures`` of shioaji 1.7.9: roots as keys, ``FuturesInfo`` rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._by_root: dict[str, list[SimpleNamespace]] = {}
        for row in rows:
            # 1.7.9 ``FuturesInfo``: ``code``, ``root``, ``delivery_date`` as a date;
            # no ``category`` and no ``symbol``.
            info = SimpleNamespace(code=row["code"], root=row["root"], delivery_date=_delivery(row), name=row["name"])
            self._by_root.setdefault(row["root"], []).append(info)

    def keys(self):  # noqa: ANN201 — mirrors the SDK mapping
        return self._by_root.keys()

    def __getitem__(self, root: str) -> list[SimpleNamespace]:
        return self._by_root[root]


class _EmptyBroker:
    def get_positions(self) -> list[Any]:
        return []


@pytest.fixture
def drill_pool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cache = tmp_path / "contracts.json"
    cache.write_text(json.dumps({"cache_version": 1, "contracts": _rows()}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "symbols.list").write_text("\n".join(FUTURES_LIST_LINES) + "\n", encoding="utf-8")
    canonical = tmp_path / "symbols.yaml"
    canonical.write_text(yaml.safe_dump({"symbols": []}), encoding="utf-8")
    monkeypatch.setenv("HFT_CONTRACT_CACHE_PATH", str(cache))
    monkeypatch.setenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", str(tmp_path / "runtime.yaml"))
    monkeypatch.setenv("HFT_UNIVERSE_LAST_GOOD_PATH", str(tmp_path / "universe_last_good.yaml"))
    return lambda: QuoteConnectionPool(str(canonical), {}, num_conns=2)


def _universe(pool: QuoteConnectionPool) -> set[str]:
    return {str(sym["code"]) for sym in pool._all_symbols}


def _drive(
    pool: QuoteConnectionPool,
    monkeypatch: pytest.MonkeyPatch,
    steps: list[dt.datetime],
) -> list[tuple[dt.datetime, set[str], bool]]:
    """Run the pool's roll exactly when its clock would, return (time, universe, adopted) per step."""
    history = []
    for local in steps:
        now_ns = _ns(local)
        monkeypatch.setattr(symbols_types, "exchange_today", lambda d=local.date(): d)
        adopted = False
        if not _session_open_or_lead(local):
            adopted = pool.roll_universe(live=False, now_ns=now_ns)
        history.append((local, _universe(pool), adopted))
    return history


def test_drill_list_matches_the_production_symbols_list() -> None:
    production = (REPO / "config/symbols.list").read_text(encoding="utf-8").splitlines()
    futures = [line for line in production if line.split("@", 1)[0] in ROOTS]
    assert futures == FUTURES_LIST_LINES


def test_fixture_is_the_recorded_179_row_shape() -> None:
    """Guard against someone "simplifying" the fixture into the shape the code expects."""
    rows = _rows()
    assert {tuple(sorted(r)) for r in rows} == {("code", "delivery_date", "exchange", "name", "root", "type")}
    assert {r["code"] for r in rows if _is_alias(r["code"])} == {f"{root}R{n}" for root in ROOTS for n in (1, 2)}


@pytest.mark.parametrize(
    ("start", "end", "settling"),
    [
        pytest.param(
            dt.datetime(2026, 11, 17, 6, 0, tzinfo=TAIPEI),
            dt.datetime(2026, 11, 19, 9, 0, tzinfo=TAIPEI),
            "K6",
            id="2026-11-18",
        ),
        pytest.param(
            dt.datetime(2026, 12, 15, 6, 0, tzinfo=TAIPEI),
            dt.datetime(2026, 12, 17, 9, 0, tzinfo=TAIPEI),
            "L6",
            id="2026-12-16",
        ),
    ],
)
def test_universe_follows_the_front_month_across_a_settlement(
    drill_pool, monkeypatch: pytest.MonkeyPatch, start: dt.datetime, end: dt.datetime, settling: str
) -> None:
    pool = drill_pool()
    history = _drive(pool, monkeypatch, _steps(start, end))

    # Every adopted universe is free of settled contracts at the moment it is adopted.
    for local, universe, adopted in history:
        if adopted:
            stale = sorted(code for code in universe if _settled(code, _ns(local)))
            assert stale == [], f"{local:%m-%d %H:%M} adopted a settled contract: {stale}"

    # Before the delivery-day final close the settling month is subscribed.
    before = [universe for local, universe, _ in history if local.date() < start.date() + dt.timedelta(days=1)]
    assert before and all(f"TMF{settling}" in universe for universe in before)

    # By the next day's pre-open the universe holds every root's new front month
    # and nothing settled. On main this happens in the 05:00-08:30 window.
    final_local, final_universe, _ = history[-1]
    final_ns = _ns(final_local)
    for root in ROOTS:
        assert _front_by_broker(root, final_ns) in final_universe, f"{root} front month missing at {final_local}"
    assert not [code for code in final_universe if _settled(code, final_ns)]


@pytest.mark.parametrize(
    ("delivery", "old", "new"),
    [
        pytest.param(dt.date(2026, 11, 18), "TMFK6", "TMFL6", id="2026-11-18"),
        pytest.param(dt.date(2026, 12, 16), "TMFL6", "TMFA7", id="2026-12-16"),
    ],
)
def test_strategy_binding_and_session_gate_follow_the_front_month(delivery: dt.date, old: str, new: str) -> None:
    """R47 trades ``TMF:R1``; the binding must move at 13:30 and the gate must let the new month through."""
    resolver = ContractFamilyResolver()
    populator = ShioajiFamilyPopulator(resolver)
    api = SimpleNamespace(Contracts=SimpleNamespace(Futures=_Futures(_rows())))
    tmf_r1 = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)

    governor = SessionGovernor(config_path=SESSION_GOVERNOR_YAML)
    gate = governor.track_gate
    for track in ("futures_day", "futures_night"):
        gate.set_track_phase(track, SessionPhase.OPEN)

    start = dt.datetime.combine(delivery - dt.timedelta(days=1), dt.time(8, 45), tzinfo=TAIPEI)
    populator.populate(api, now_ns=_ns(start))
    for local in _steps(start, dt.datetime.combine(delivery + dt.timedelta(days=1), dt.time(9, 0), tzinfo=TAIPEI)):
        now_ns = _ns(local)
        populator.roll_if_due(now_ns=now_ns)
        bound = resolver.resolve_family(tmf_r1).display()
        assert bound == _front_by_broker("TMF", now_ns), f"R1 bound to {bound} at {local:%m-%d %H:%M}"
        assert gate.get_phase(bound) is SessionPhase.OPEN, f"TrackGate blocks {bound} at {local:%m-%d %H:%M}"

    at_close = _ns(dt.datetime.combine(delivery, dt.time(13, 29), tzinfo=TAIPEI))
    after_close = _ns(dt.datetime.combine(delivery, dt.time(13, 30), tzinfo=TAIPEI))
    assert _front_by_broker("TMF", at_close) == old
    assert _front_by_broker("TMF", after_close) == new


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("local", "dropped"),
    [
        pytest.param(dt.datetime(2026, 11, 18, 13, 29, tzinfo=TAIPEI), False, id="final-session"),
        pytest.param(dt.datetime(2026, 11, 18, 13, 30, tzinfo=TAIPEI), True, id="after-final-close"),
        pytest.param(dt.datetime(2026, 11, 19, 8, 0, tzinfo=TAIPEI), True, id="next-morning"),
    ],
)
async def test_startup_recovery_drops_the_settled_lot_only_after_the_final_close(
    monkeypatch: pytest.MonkeyPatch, local: dt.datetime, dropped: bool
) -> None:
    from hft_platform.execution.checkpoint import PositionCheckpointWriter

    monkeypatch.setattr(timebase, "now_ns", lambda: _ns(local))
    checkpoint = {
        "ACC1:R47_MAKER_TMF:TMFK6": {
            "symbol": "TMFK6",
            "net_qty": 1,
            "avg_price_scaled": 331_000_000,
            "realized_pnl_scaled": 0,
        }
    }
    store = PositionStore()
    verifier = StartupPositionVerifier(_EmptyBroker(), store, checkpoint_path="/fake", order_mode="sim")
    with patch.object(
        PositionCheckpointWriter,
        "load_checkpoint",
        return_value={
            "trading_date": local.strftime("%Y%m%d"),
            "positions": checkpoint,
            "total_realized_pnl_scaled": 0,
        },
    ):
        result = await verifier.recover(trading_date=local.strftime("%Y%m%d"), account_id="ACC1")

    actions = [m["action"] for m in result.mismatches]
    assert ("dropped_settled" in actions) is dropped


def _expiry_window_start(code: str, delivery: dt.date) -> int:
    morning = _ns(dt.datetime.combine(delivery, dt.time(9, 0), tzinfo=TAIPEI))
    start, _end = expiry_window_ns(code, morning, DEFAULT_EXPIRY_CUTOFF_MINUTE)
    return start


def _third_wednesday(day: dt.date) -> dt.date:
    first = day.replace(day=1)
    return first + dt.timedelta(days=(2 - first.weekday()) % 7 + 14)


_MONTH_ROWS = [r for r in _rows() if not _is_alias(r["code"])]
_ON_WEDNESDAY = [r["code"] for r in _MONTH_ROWS if _delivery(r) == _third_wednesday(_delivery(r))]
# 2027-09-15 is the Mid-Autumn Festival: the broker lists every September-2027
# contract as delivering on Thursday 09-16.
_HOLIDAY_SHIFTED = [r["code"] for r in _MONTH_ROWS if _delivery(r) != _third_wednesday(_delivery(r))]


def test_fixture_holds_a_holiday_shifted_delivery() -> None:
    assert sorted(_HOLIDAY_SHIFTED) == ["MXFI7", "TMFI7", "TXFI7"]


@pytest.mark.parametrize("code", _ON_WEDNESDAY)
def test_order_path_expiry_window_opens_on_the_recorded_delivery_date(code: str) -> None:
    """``risk/expiry`` derives the date from the code; it must agree with the broker row."""
    delivery = _delivery(next(r for r in _rows() if r["code"] == code))
    expected = dt.datetime.combine(delivery, dt.time(13, 0), tzinfo=TAIPEI)
    assert _expiry_window_start(code, delivery) == _ns(expected)


@pytest.mark.parametrize("code", _HOLIDAY_SHIFTED)
def test_order_path_expiry_window_follows_a_holiday_shifted_delivery(code: str) -> None:
    """2027-09-15 is Mid-Autumn; the broker row delivers 09-16 and so must the order path."""
    delivery = _delivery(next(r for r in _rows() if r["code"] == code))
    expected = dt.datetime.combine(delivery, dt.time(13, 0), tzinfo=TAIPEI)
    assert _expiry_window_start(code, delivery) == _ns(expected)


# --------------------------------------------------------------------------- #
# The next three settlements from today, every day CI runs                     #
# --------------------------------------------------------------------------- #
#
# The recorded fixture covers 2026-11 and 2026-12. The rule below rebuilds a
# listing in the same row shape for whatever months come next, so the drill
# keeps crossing a real upcoming settlement (holidays included, from the XTAI
# calendar) instead of going stale with its fixture.

_MONTH_LETTERS = "ABCDEFGHIJKL"
_NAMES = {"TMF": "微型臺指期貨", "TXF": "臺股期貨", "MXF": "小型臺指期貨"}


def _holiday_shifted_delivery(year: int, month: int) -> dt.date:
    day = _third_wednesday(dt.date(year, month, 1))
    calendar = get_calendar()
    for _ in range(14):
        if calendar.is_trading_day(day):
            return day
        day += dt.timedelta(days=1)
    return _third_wednesday(dt.date(year, month, 1))


def _listing_from(as_of: dt.date) -> list[dict[str, Any]]:
    """Three consecutive months plus the next three quarter months, as TAIFEX lists them."""
    months: list[tuple[int, int]] = []
    year, month = as_of.year, as_of.month
    if _holiday_shifted_delivery(year, month) < as_of:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    y, m = year, month
    while len(months) < 3:
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    while len(months) < 6:
        if m % 3 == 0:
            months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    rows: list[dict[str, Any]] = []
    for root in ROOTS:
        for index, (y, m) in enumerate(months):
            delivery = _holiday_shifted_delivery(y, m).strftime("%Y/%m/%d")
            code = f"{root}{_MONTH_LETTERS[m - 1]}{y % 10}"
            rows.append(
                {
                    "code": code,
                    "name": f"{_NAMES[root]} {y}{m:02d}",
                    "exchange": "TAIFEX",
                    "type": "future",
                    "root": root,
                    "delivery_date": delivery,
                }
            )
            if index < 2:
                alias_name = "近月" if index == 0 else "次月"
                rows.append(dict(rows[-1], code=f"{root}R{index + 1}", name=f"{_NAMES[root]} {alias_name}"))
    return rows


def _upcoming_deliveries(count: int) -> list[dt.date]:
    today = dt.datetime.now(TAIPEI).date()
    out: list[dt.date] = []
    year, month = today.year, today.month
    while len(out) < count:
        delivery = _holiday_shifted_delivery(year, month)
        if delivery >= today:
            out.append(delivery)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def test_synthesized_listing_has_the_recorded_row_shape() -> None:
    recorded = {tuple(sorted(r)) for r in _rows()}
    synthesized = {tuple(sorted(r)) for r in _listing_from(dt.date(2026, 11, 3))}
    assert synthesized == recorded
    # On the recorded day the rule reproduces the recorded listing exactly.
    assert sorted((r["code"], r["delivery_date"]) for r in _listing_from(dt.date(2026, 11, 3))) == sorted(
        (r["code"], r["delivery_date"]) for r in _rows()
    )


@pytest.mark.parametrize("nth", [0, 1, 2], ids=["next", "next+1", "next+2"])
def test_universe_and_binding_cross_the_upcoming_settlement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nth: int
) -> None:
    delivery = _upcoming_deliveries(3)[nth]
    rows = _listing_from(delivery - dt.timedelta(days=1))
    cache = tmp_path / "contracts.json"
    cache.write_text(json.dumps({"cache_version": 1, "contracts": rows}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "symbols.list").write_text("\n".join(FUTURES_LIST_LINES) + "\n", encoding="utf-8")
    canonical = tmp_path / "symbols.yaml"
    canonical.write_text(yaml.safe_dump({"symbols": []}), encoding="utf-8")
    monkeypatch.setenv("HFT_CONTRACT_CACHE_PATH", str(cache))
    monkeypatch.setenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", str(tmp_path / "runtime.yaml"))
    monkeypatch.setenv("HFT_UNIVERSE_LAST_GOOD_PATH", str(tmp_path / "universe_last_good.yaml"))
    pool = QuoteConnectionPool(str(canonical), {}, num_conns=2)

    def front(root: str, now_ns: int) -> str:
        cutoff = taifex_delivery_cutoff(now_ns)
        live = [r for r in rows if r["root"] == root and not _is_alias(r["code"]) and _delivery(r) >= cutoff]
        return min(live, key=lambda r: (_delivery(r), r["code"]))["code"]

    resolver = ContractFamilyResolver()
    populator = ShioajiFamilyPopulator(resolver)
    tmf_r1 = ContractFamily(Product.FUTURE, "TMF", FamilyCode.R1)
    start = dt.datetime.combine(delivery - dt.timedelta(days=1), dt.time(6, 0), tzinfo=TAIPEI)
    end = dt.datetime.combine(delivery + dt.timedelta(days=1), dt.time(9, 0), tzinfo=TAIPEI)
    populator.populate(SimpleNamespace(Contracts=SimpleNamespace(Futures=_Futures(rows))), now_ns=_ns(start))

    history = _drive(pool, monkeypatch, _steps(start, end))
    for local, universe, adopted in history:
        now_ns = _ns(local)
        populator.roll_if_due(now_ns=now_ns)
        assert resolver.resolve_family(tmf_r1).display() == front("TMF", now_ns), f"R1 wrong at {local}"
        if adopted:
            settled = [
                c
                for c in universe
                if _delivery(next(r for r in rows if r["code"] == c)) < taifex_delivery_cutoff(now_ns)
            ]
            assert settled == [], f"{local} adopted settled {settled}"

    final_local, final_universe, _ = history[-1]
    for root in ROOTS:
        assert front(root, _ns(final_local)) in final_universe, f"{root} front missing after {delivery}"
