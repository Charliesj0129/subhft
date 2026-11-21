"""Incremental ingest flow with fake SDK objects and a fake ClickHouse client: no network, no real sleeps."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from research.data_pipeline.hist_ingest import __main__ as cli
from research.data_pipeline.hist_ingest import fubon_source, shioaji_source
from research.data_pipeline.hist_ingest.model import TAIPEI, SourceError
from research.data_pipeline.hist_ingest.ratelimit import RateLimiter
from research.data_pipeline.hist_ingest.store import HistStore

NS = 1_000_000_000
MB = 1024 * 1024
FRI, THU, WED, TUE, MON = (dt.date(2026, 11, d) for d in (20, 19, 18, 17, 16))


def local_ns(day: dt.date, hhmm: str) -> int:
    """Shioaji ts: Taipei wall clock expressed as if it were UTC."""
    return int(dt.datetime.fromisoformat(f"{day}T{hhmm}:00+00:00").timestamp()) * NS


class FakeClient:
    """Records inserts in order and answers the watermark query from what was inserted."""

    def __init__(self):
        self.calls, self.marks = [], {}

    def insert(self, table, data, column_names=None):
        self.calls.append((table, list(data), list(column_names)))
        if table == "hft.hist_ingest_watermark":
            for row in data:
                self.marks[(row[0], row[1], row[2], row[3])] = row[4]

    def query(self, sql, parameters=None):
        p = parameters
        keep = [(k[3], v) for k, v in self.marks.items() if k[:3] == (p["source"], p["product"], p["kind"])]
        return SimpleNamespace(result_rows=keep)

    def inserted(self, table):
        return [row for t, data, _ in self.calls if t == table for row in data]


class FakeShioaji:
    """usage() walks through ``remaining`` (the last value repeats); ticks() returns ticks_for(day)."""

    def __init__(self, remaining=(500 * MB,), target_code="TXFL6", n_ticks=3):
        self.remaining, self.n_ticks, self.requested_days = list(remaining), n_ticks, []
        self.Contracts = SimpleNamespace(
            Futures={f"{p}R1": SimpleNamespace(target_code=target_code) for p in ("TXF", "MXF", "TMF")}
        )

    def usage(self):
        left = self.remaining.pop(0) if len(self.remaining) > 1 else self.remaining[0]
        return SimpleNamespace(remaining_bytes=left)

    def ticks(self, contract, date, query_type=None):
        self.requested_days.append(dt.date.fromisoformat(date))
        day, n = dt.date.fromisoformat(date), self.n_ticks
        return SimpleNamespace(
            ts=[local_ns(day, "09:00") + i for i in range(n)],
            close=[17660.5 + i for i in range(n)],
            volume=[1] * n,
            bid_price=[17660.0] * n,
            ask_price=[17661.0] * n,
            tick_type=[1] * n,
        )

    def kbars(self, contract, start, end):
        day = dt.date.fromisoformat(start)
        return SimpleNamespace(ts=[local_ns(day, "09:00")], Open=[1.5], High=[2.5], Low=[1.0], Close=[2.0], Volume=[7])


def setup(api=None, **kw):
    client = FakeClient()
    src = shioaji_source.ShioajiSource(api or FakeShioaji(), limiter=RateLimiter(10**6, 1.0))
    return src, HistStore(client, dry_run=kw.pop("dry_run", False)), client


def run_ticks(src, store, since=MON, until=FRI, **kw):
    opts = {"min_remaining_bytes": 50 * MB, "max_calls": 20, "force": False} | kw
    return shioaji_source.ingest_ticks(src, store, products=["TXF"], since=since, until=until, **opts)


def test_done_and_empty_days_are_skipped_and_the_rest_run_newest_first():
    src, store, client = setup()
    client.marks |= {("shioaji", "TXF", "ticks", WED): "DONE", ("shioaji", "TXF", "ticks", TUE): "EMPTY"}
    run_ticks(src, store)
    assert src.api.requested_days == [FRI, THU, MON]


def test_force_refetches_days_that_are_already_done():
    src, store, client = setup()
    client.marks |= {("shioaji", "TXF", "ticks", d): "DONE" for d in (FRI, THU)}
    run_ticks(src, store, since=THU, force=True)
    assert src.api.requested_days == [FRI, THU]


def test_tick_call_cap_stops_the_run():
    src, store, _ = setup()
    stats = run_ticks(src, store, max_calls=2)
    assert src.api.requested_days == [FRI, THU]
    assert stats["calls"] == 2


def test_low_quota_before_the_call_stops_and_marks_pending_quota():
    src, store, client = setup(FakeShioaji(remaining=(10 * MB,)))
    run_ticks(src, store)
    assert src.api.requested_days == []
    assert client.marks == {("shioaji", "TXF", "ticks", FRI): "PENDING_QUOTA"}


def test_quota_running_out_midway_keeps_the_finished_day_and_stops():
    src, store, client = setup(FakeShioaji(remaining=(500 * MB, 400 * MB, 10 * MB)))
    run_ticks(src, store)
    assert src.api.requested_days == [FRI]
    assert client.marks == {("shioaji", "TXF", "ticks", FRI): "DONE", ("shioaji", "TXF", "ticks", THU): "PENDING_QUOTA"}


def test_empty_result_with_low_remaining_is_pending_quota_not_empty():
    src, store, client = setup(FakeShioaji(remaining=(500 * MB, 10 * MB), n_ticks=0))
    run_ticks(src, store)
    assert client.marks == {("shioaji", "TXF", "ticks", FRI): "PENDING_QUOTA"}


def test_empty_result_with_high_remaining_is_empty():
    src, store, client = setup(FakeShioaji(n_ticks=0))
    run_ticks(src, store, until=THU, since=THU)
    assert client.marks == {("shioaji", "TXF", "ticks", THU): "EMPTY"}
    assert client.inserted("hft.hist_ticks") == []


def test_tick_contract_code_follows_the_roll_rule_for_an_old_day():
    src, store, client = setup()
    old_day = dt.date(2026, 10, 20)
    run_ticks(src, store, until=old_day, since=old_day)
    row = client.inserted("hft.hist_ticks")[0]
    assert (row.query_code, row.contract_code) == ("TXFR1", "TXFJ6")
    assert client.marks == {("shioaji", "TXF", "ticks", old_day): "DONE"}


@pytest.mark.parametrize(
    ("day", "code"),
    [
        (dt.date(2026, 11, 18), "TMFK6"),
        (dt.date(2026, 11, 19), "TMFL6"),
        (dt.date(2026, 12, 16), "TMFL6"),
        (dt.date(2026, 12, 17), "TMFA7"),
    ],
)
def test_front_month_code_rolls_after_the_third_wednesday(day, code):
    from research.data_pipeline.hist_ingest.model import front_month_code

    assert front_month_code("TMF", day) == code


def test_contract_code_comes_from_target_code_and_falls_back_to_the_query_code():
    src, store, client = setup()
    run_ticks(src, store, until=FRI, since=FRI)
    row = client.inserted("hft.hist_ticks")[0]
    assert (row.query_code, row.contract_code) == ("TXFR1", "TXFL6")
    assert src.contract("TXF")[2] == "TXFL6"
    fallback = shioaji_source.ShioajiSource(FakeShioaji(target_code=""), limiter=RateLimiter(5, 1.0))
    assert fallback.contract("TXF")[2] == "TXFR1"


def test_tick_rows_are_scaled_utc_and_classified_by_session():
    src, store, client = setup()
    run_ticks(src, store, until=FRI, since=FRI)
    first = client.inserted("hft.hist_ticks")[0]
    assert first.exch_ts == int(dt.datetime(2026, 11, 20, 1, 0, tzinfo=dt.UTC).timestamp()) * NS
    assert (first.price_scaled, first.bid_scaled, first.session, first.trading_day) == (
        17660500000,
        17660000000,
        "day",
        FRI,
    )


def test_rows_are_inserted_before_the_done_watermark():
    src, store, client = setup()
    run_ticks(src, store, until=FRI, since=FRI)
    assert [t for t, _, _ in client.calls] == ["hft.hist_ticks", "hft.hist_ingest_watermark"]
    assert client.calls[1][1][0].status == "DONE"
    assert client.calls[1][1][0].rows == 3


def test_insert_batches_are_capped_and_use_the_migration_column_names():
    src, store, client = setup(FakeShioaji(n_ticks=5))
    store.batch_rows = 2
    run_ticks(src, store, until=FRI, since=FRI)
    tick_calls = [(len(d), cols) for t, d, cols in client.calls if t == "hft.hist_ticks"]
    assert [n for n, _ in tick_calls] == [2, 2, 1]
    assert tick_calls[0][1][:3] == ["source", "product", "query_code"]


def test_dry_run_converts_and_counts_but_writes_nothing():
    src, store, client = setup(dry_run=True)
    stats = run_ticks(src, store, until=FRI, since=FRI)
    assert stats["rows"] == 3
    assert client.calls == []


def test_rerunning_a_day_yields_identical_unique_row_keys():
    src, store, client = setup()
    for _ in range(2):
        run_ticks(src, store, until=FRI, since=FRI, force=True)
    rows = client.inserted("hft.hist_ticks")
    keys = [(r.source, r.product, r.trading_day, r.exch_ts, r.seq) for r in rows]
    assert len(keys) == 6
    assert len(set(keys)) == 3
    assert keys[:3] == keys[3:]


def test_a_failed_tick_query_is_marked_failed_and_the_next_day_still_runs():
    src, store, client = setup()
    real = src.api.ticks

    def flaky(contract, date, query_type=None):
        if date == str(FRI):
            raise RuntimeError("boom")
        return real(contract, date, query_type)

    src.api.ticks = flaky
    run_ticks(src, store, since=THU)
    assert client.marks[("shioaji", "TXF", "ticks", FRI)] == "FAILED"
    assert client.marks[("shioaji", "TXF", "ticks", THU)] == "DONE"


class RateLimitedError(Exception):
    status_code = 429


class FakeFubonRest:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []
        self.historical = SimpleNamespace(candles=self.candles)

    def candles(self, **params):
        self.calls.append(params)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def candle(day, hhmm, close="17660.5"):
    return {
        "date": f"{day}T{hhmm}:00.000+08:00",
        "open": 17660.0,
        "high": 17661.0,
        "low": 17659.5,
        "close": close,
        "volume": 5,
    }


def fubon_setup(outcomes):
    rest, slept = FakeFubonRest(outcomes), []
    src = fubon_source.FubonSource(None, rest, limiter=RateLimiter(10**6, 1.0), sleep=slept.append)
    return src, rest, slept


def test_fubon_429_is_retried_after_61_seconds_then_succeeds():
    src, _, slept = fubon_setup([RateLimitedError(), {"data": [candle(FRI, "09:00")]}])
    resp = src.candles("TXF", "regular", FRI, FRI)
    assert resp["data"][0]["close"] == "17660.5"
    assert slept == [61.0]


def test_fubon_three_429_retries_exhausted_marks_the_window_failed():
    src, rest, slept = fubon_setup([{"statusCode": 429}])
    store = HistStore(FakeClient())
    fubon_source.ingest_kbars(src, store, products=["TXF"], since=THU, until=FRI, force=False)
    assert len(rest.calls) == 4
    assert slept == [61.0] * 3
    assert {v for v in store.client.marks.values()} == {"FAILED"}
    assert store.client.inserted("hft.hist_kbars") == []


def test_fubon_candles_are_scaled_utc_and_carry_no_contract_code_without_a_month_symbol():
    code, rows = fubon_source.convert_candles(
        {"data": [candle(FRI, "21:00", close=17660.1)]}, product="TXF", ingest_ts=1
    )
    assert code == ""
    assert (rows[0].query_code, rows[0].close_scaled, rows[0].session) == ("1!", 17660100000, "night")
    assert rows[0].bar_ts == int(dt.datetime(2026, 11, 20, 13, 0, tzinfo=dt.UTC).timestamp()) * NS
    assert fubon_source.convert_candles({"symbol": "TXFL6", "data": []}, product="TXF", ingest_ts=1)[0] == "TXFL6"


def test_fubon_candle_missing_a_price_field_raises_source_error():
    with pytest.raises(SourceError):
        fubon_source.convert_candles(
            {"data": [{"date": "2026-11-20T09:00:00+08:00", "open": 1}]}, product="TXF", ingest_ts=1
        )


def test_kbar_window_marks_every_weekday_and_a_second_run_fetches_nothing():
    src, rest, _ = fubon_setup([{"data": [candle(WED, "09:00"), candle(FRI, "09:00")]}])
    store = HistStore(FakeClient())
    for _ in range(2):
        fubon_source.ingest_kbars(src, store, products=["TXF"], since=MON, until=FRI, force=False)
    assert len(rest.calls) == 2  # regular + afterhours of ONE window; the second run found nothing pending
    assert [p["session"] for p in rest.calls] == ["regular", "afterhours"]
    assert (rest.calls[0]["from"], rest.calls[0]["to"], rest.calls[0]["contractMonth"]) == (
        "2026-11-16",
        "2026-11-20",
        "1!",
    )
    status = {d: v for (_, _, _, d), v in store.client.marks.items()}
    assert status == {MON: "EMPTY", TUE: "EMPTY", WED: "DONE", THU: "EMPTY", FRI: "DONE"}


def test_shioaji_kbars_low_quota_marks_the_window_pending_quota_and_calls_nothing():
    src, store, client = setup(FakeShioaji(remaining=(MB,)))
    stats = shioaji_source.ingest_kbars(
        src, store, products=["TXF"], since=THU, until=FRI, min_remaining_bytes=50 * MB, force=False
    )
    assert stats["stopped"] == 1
    assert set(client.marks.values()) == {"PENDING_QUOTA"}
    assert client.inserted("hft.hist_kbars") == []


def test_shioaji_kbars_end_label_is_stored_as_the_utc_bar_start():
    src, store, client = setup()
    shioaji_source.ingest_kbars(
        src, store, products=["TXF"], since=FRI, until=FRI, min_remaining_bytes=50 * MB, force=False
    )
    row = client.inserted("hft.hist_kbars")[0]
    assert (row.open_scaled, row.high_scaled, row.low_scaled, row.close_scaled, row.contract_code) == (
        1500000,
        2500000,
        1000000,
        2000000,
        "TXFL6",
    )
    assert row.bar_ts == int(dt.datetime(2026, 11, 20, 0, 59, tzinfo=dt.UTC).timestamp()) * NS


def test_cli_refuses_shioaji_ticks_at_ten_on_a_weekday_before_connecting(monkeypatch):
    monkeypatch.setattr(cli.HistStore, "from_env", lambda dry_run=False: pytest.fail("connected to ClickHouse"))
    now = dt.datetime(2026, 11, 20, 10, 0, tzinfo=TAIPEI)
    assert cli.main(["ticks", "--env-file", "/nonexistent"], now=now) == 2


def test_cli_ticks_run_at_1530_and_always_log_out(monkeypatch):
    api, closed = FakeShioaji(), []
    src = shioaji_source.ShioajiSource(api, limiter=RateLimiter(10**6, 1.0))
    src.close = lambda: closed.append(True)
    client = FakeClient()
    monkeypatch.setattr(cli.HistStore, "from_env", lambda dry_run=False: HistStore(client, dry_run=dry_run))
    monkeypatch.setattr(cli.shioaji_source.ShioajiSource, "connect", lambda: src)
    now = dt.datetime(2026, 11, 20, 15, 30, tzinfo=TAIPEI)
    code = cli.main(["ticks", "--products", "txf", "--since", "2026-11-20", "--env-file", "/nonexistent"], now=now)
    assert code == 0
    assert api.requested_days == [FRI]
    assert closed == [True]


def test_cli_returns_nonzero_and_still_logs_out_when_the_usage_query_fails(monkeypatch):
    api, closed = FakeShioaji(), []
    api.usage = lambda: (_ for _ in ()).throw(RuntimeError("usage down"))
    src = shioaji_source.ShioajiSource(api, limiter=RateLimiter(10**6, 1.0))
    src.close = lambda: closed.append(True)
    client = FakeClient()
    monkeypatch.setattr(cli.HistStore, "from_env", lambda dry_run=False: HistStore(client, dry_run=dry_run))
    monkeypatch.setattr(cli.shioaji_source.ShioajiSource, "connect", lambda: src)
    now = dt.datetime(2026, 11, 20, 15, 30, tzinfo=TAIPEI)
    assert cli.main(["ticks", "--since", "2026-11-20", "--env-file", "/nonexistent"], now=now) == 1
    assert (api.requested_days, closed, client.calls) == ([], [True], [])
