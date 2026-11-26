"""Incremental ingest flow with fake SDK objects and a fake ClickHouse client: no network, no real sleeps."""

from __future__ import annotations

import datetime as dt
import os
from types import SimpleNamespace

import pytest

from research.data_pipeline.hist_ingest import __main__ as cli
from research.data_pipeline.hist_ingest import fubon_source, shioaji_source
from research.data_pipeline.hist_ingest import store as store_module
from research.data_pipeline.hist_ingest.model import TAIPEI, SourceError
from research.data_pipeline.hist_ingest.ratelimit import RateLimiter
from research.data_pipeline.hist_ingest.store import HistStore, WindowResult, WriterBusyError, run_kbar_windows

NS = 1_000_000_000
MB = 1024 * 1024
FRI, THU, WED, TUE, MON = (dt.date(2026, 11, d) for d in (20, 19, 18, 17, 16))


@pytest.fixture(autouse=True)
def isolate_writer_locks(tmp_path, monkeypatch):
    monkeypatch.setenv("HFT_HIST_INGEST_LOCK_DIR", str(tmp_path / "locks"))


def local_ns(day: dt.date, hhmm: str) -> int:
    """Shioaji ts: Taipei wall clock expressed as if it were UTC."""
    return int(dt.datetime.fromisoformat(f"{day}T{hhmm}:00+00:00").timestamp()) * NS


class FakeClient:
    """Records inserts in order and answers the watermark query from what was inserted."""

    def __init__(self):
        self.calls, self.marks = [], {}
        self.tables, self.commands, self.events = {}, [], []
        self.fail_delete = False
        self.fail_insert_at = None
        self.data_inserts = 0
        self.queries = []

    def insert(self, table, data, column_names=None):
        self.events.append(("insert", table))
        if table != "hft.hist_ingest_watermark":
            self.data_inserts += 1
            if self.data_inserts == self.fail_insert_at:
                raise RuntimeError("insert failed")
        self.calls.append((table, list(data), list(column_names)))
        self.tables.setdefault(table, []).extend(data)
        if table == "hft.hist_ingest_watermark":
            for row in data:
                self.marks[(row[0], row[1], row[2], row[3])] = row[4]

    def command(self, sql, parameters=None, settings=None):
        self.commands.append((sql, parameters, settings))
        self.events.append(("delete", sql.split()[2]))
        if self.fail_delete:
            raise RuntimeError("delete failed")
        assert settings == {"mutations_sync": 1}
        table = sql.split()[2]
        p = parameters

        def deleted(row):
            if row.ingest_ts >= p.get("threshold", float("inf")):
                return False
            if (row.source, row.product) != (p["source"], p["product"]):
                return False
            if table == "hft.hist_ticks":
                return p["start"] <= row.trading_day <= p["end"]
            assert table == "hft.hist_kbars"
            return row.timeframe_min == p["timeframe"] and p["start"] <= row.bar_ts < p["end"]

        self.tables[table] = [row for row in self.tables.get(table, []) if not deleted(row)]

    def final(self, table):
        # ReplacingMergeTree FINAL only replaces matching order keys.
        keys = (
            ("source", "product", "trading_day", "exch_ts", "seq")
            if table == "hft.hist_ticks"
            else ("source", "product", "timeframe_min", "bar_ts")
        )
        latest = {}
        for row in self.tables.get(table, []):
            key = tuple(getattr(row, field) for field in keys)
            if key not in latest or row.ingest_ts >= latest[key].ingest_ts:
                latest[key] = row
        return list(latest.values())

    def query(self, sql, parameters=None):
        self.queries.append((sql, parameters))
        p = parameters
        if sql.startswith("SELECT max(ingest_ts)"):
            table = sql.split(" FROM ")[1].split()[0]
            values = [
                r.ingest_ts
                for r in self.tables.get(table, [])
                if (r.source, r.product) == (p["source"], p["product"])
                and (
                    p["start"] <= r.trading_day <= p["end"]
                    if table == "hft.hist_ticks"
                    else r.timeframe_min == p["timeframe"] and p["start"] <= r.bar_ts < p["end"]
                )
            ]
            return SimpleNamespace(result_rows=[(max(values, default=0),)])
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
        day = dt.date.fromisoformat(end)
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
    assert [t for t, _, _ in client.calls][-2:] == ["hft.hist_ticks", "hft.hist_ingest_watermark"]
    assert client.calls[-1][1][0].status == "DONE"
    assert client.calls[-1][1][0].rows == 3


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


@pytest.mark.parametrize(
    "reply",
    [{"statusCode": 401, "message": "Unauthorized"}, {"statusCode": 500}, {"symbol": "TXF"}],
)
def test_fubon_error_or_dataless_reply_marks_the_window_failed_not_empty(reply):
    src, _, _ = fubon_setup([reply])
    store = HistStore(FakeClient())
    fubon_source.ingest_kbars(src, store, products=["TXF"], since=THU, until=FRI, force=False)
    assert set(store.client.marks.values()) == {"FAILED"}


def test_friday_night_bars_after_midnight_count_toward_monday():
    from research.data_pipeline.hist_ingest.model import trading_day_of

    sat_0100 = int(dt.datetime(2026, 11, 20, 17, 0, tzinfo=dt.UTC).timestamp()) * NS
    thu_2100 = int(dt.datetime(2026, 11, 19, 13, 0, tzinfo=dt.UTC).timestamp()) * NS
    fri_0900 = int(dt.datetime(2026, 11, 20, 1, 0, tzinfo=dt.UTC).timestamp()) * NS
    assert trading_day_of(sat_0100) == dt.date(2026, 11, 23)
    assert trading_day_of(thu_2100) == FRI
    assert trading_day_of(fri_0900) == FRI


def test_tick_seq_counts_within_one_timestamp_so_a_dropped_tick_keeps_other_keys():
    def raw(ts_list):
        n = len(ts_list)
        return {
            "ts": ts_list,
            "close": [1] * n,
            "volume": [1] * n,
            "bid_price": [1] * n,
            "ask_price": [1] * n,
            "tick_type": [1] * n,
        }

    base = local_ns(FRI, "09:00")
    full = shioaji_source.convert_ticks(raw([base, base + 1, base + 1, base + 2]), "TXF", "TXFR1", "TXFL6", FRI, 1)
    short = shioaji_source.convert_ticks(raw([base + 1, base + 1, base + 2]), "TXF", "TXFR1", "TXFL6", FRI, 1)
    keys = {(r.exch_ts, r.seq) for r in full}
    assert {(r.exch_ts, r.seq) for r in short} <= keys
    assert [r.seq for r in full] == [0, 0, 1, 0]


def test_env_fallback_parser_drops_inline_comments_and_keeps_quoted_hashes(tmp_path, monkeypatch):
    import builtins

    env = tmp_path / "x.env"
    env.write_text("HIST_T_A=abc # note\nHIST_T_B='p#q' # c\n", encoding="utf-8")
    for k in ("HIST_T_A", "HIST_T_B"):
        monkeypatch.delenv(k, raising=False)
    real_import = builtins.__import__

    def no_dotenv(name, *a, **kw):
        if name == "dotenv":
            raise ImportError(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_dotenv)
    cli.load_env_file(str(env))
    monkeypatch.setattr(builtins, "__import__", real_import)
    assert (os.environ["HIST_T_A"], os.environ["HIST_T_B"]) == ("abc", "p#q")


def test_kbar_window_marks_every_weekday_and_a_second_run_fetches_nothing():
    src, rest, _ = fubon_setup([{"data": [candle(WED, "09:00"), candle(FRI, "09:00")]}])
    store = HistStore(FakeClient())
    for _ in range(2):
        fubon_source.ingest_kbars(src, store, products=["TXF"], since=MON, until=FRI, force=False)
    assert len(rest.calls) == 2  # regular + afterhours of ONE window; the second run found nothing pending
    assert [p["session"] for p in rest.calls] == ["regular", "afterhours"]
    assert (rest.calls[0]["from"], rest.calls[0]["to"], rest.calls[0]["contractMonth"]) == (
        "2026-11-13",
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


@pytest.mark.parametrize("remaining", [2 * 1024 * MB, 500 * MB, 500 * MB - 1])
def test_quota_guard_uses_remaining_bytes_at_scheduled_floor(remaining):
    api = FakeShioaji(remaining=(remaining,))
    api.usage = lambda: SimpleNamespace(bytes=0, limit_bytes=2 * 1024 * MB, remaining_bytes=remaining)
    src, store, client = setup(api)
    stats = run_ticks(src, store, since=FRI, min_remaining_bytes=500 * MB)
    assert src.remaining_bytes() == remaining
    assert api.requested_days == ([FRI] if remaining >= 500 * MB else [])
    assert client.marks[("shioaji", "TXF", "ticks", FRI)] == ("DONE" if remaining >= 500 * MB else "PENDING_QUOTA")
    assert stats["calls"] == (1 if remaining >= 500 * MB else 0)


def test_pending_quota_day_retries_after_usage_replenishes():
    api = FakeShioaji(remaining=(500 * MB - 1,))
    src, store, client = setup(api)
    first = run_ticks(src, store, since=FRI, min_remaining_bytes=500 * MB)
    assert first["PENDING_QUOTA"] == 1
    assert api.requested_days == []
    api.remaining = [2 * 1024 * MB]
    second = run_ticks(src, store, since=FRI, min_remaining_bytes=500 * MB)
    assert second["calls"] == 1
    assert api.requested_days == [FRI]
    assert client.marks[("shioaji", "TXF", "ticks", FRI)] == "DONE"


def test_cli_converts_scheduled_500_mib_floor_to_bytes(monkeypatch):
    floors = []
    monkeypatch.setattr(cli.HistStore, "from_env", lambda dry_run=False: HistStore(FakeClient()))
    monkeypatch.setattr(cli.shioaji_source.ShioajiSource, "connect", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "_ingest", lambda args, src, store: floors.append(args.floor) or {})
    now = dt.datetime(2026, 11, 25, 14, 20, 6, tzinfo=TAIPEI)
    assert cli.main(["ticks", "--min-remaining-mb", "500", "--env-file", "/nonexistent"], now=now) == 0
    assert floors == [524288000]


@pytest.mark.parametrize("stats", [{"FAILED": 1}, {"failed": 1}, {"failed_products": 1}])
def test_cli_returns_nonzero_for_failed_ingest_stats(monkeypatch, stats):
    closed = []
    monkeypatch.setattr(cli.HistStore, "from_env", lambda dry_run=False: HistStore(FakeClient()))
    monkeypatch.setattr(
        cli.shioaji_source.ShioajiSource, "connect", lambda: SimpleNamespace(close=lambda: closed.append(True))
    )
    monkeypatch.setattr(cli, "_ingest", lambda args, src, store: stats)
    now = dt.datetime(2026, 11, 25, 14, 20, 6, tzinfo=TAIPEI)
    assert cli.main(["ticks", "--env-file", "/nonexistent"], now=now) == 1
    assert closed == [True]


@pytest.mark.parametrize("kind", ["ticks", "kbars"])
@pytest.mark.parametrize("reply", [None, SimpleNamespace(), {}, {"ts": []}, {"error": "unavailable"}, {"ts": 1}])
def test_shioaji_malformed_reply_marks_failed_not_empty(kind, reply):
    src, store, client = setup()
    setattr(src.api, kind, lambda **kwargs: reply)
    ingest_snapshot(kind, src, store)
    assert client.marks[("shioaji", "TXF", kind, FRI)] == "FAILED"
    assert client.final("hft.hist_" + kind) == []


@pytest.mark.parametrize(
    "kind,names",
    [
        ("ticks", ("ts", "close", "volume", "bid_price", "ask_price", "tick_type")),
        ("kbars", ("ts", "Open", "High", "Low", "Close", "Volume")),
    ],
)
@pytest.mark.parametrize("object_reply", [False, True])
@pytest.mark.parametrize("column", [[], None, {}, ""])
def test_shioaji_empty_columns_require_present_sequences(kind, names, object_reply, column):
    src, store, client = setup()
    reply = dict.fromkeys(names, column)
    if object_reply:
        reply = SimpleNamespace(**reply)
    setattr(src.api, kind, lambda **kwargs: reply)
    ingest_snapshot(kind, src, store)
    assert client.marks[("shioaji", "TXF", kind, FRI)] == ("EMPTY" if column == [] else "FAILED")


@pytest.mark.parametrize("reply", [None, SimpleNamespace(), {}, {"data": {}}, {"data": ""}, {"data": None}])
def test_fubon_malformed_container_marks_failed_not_empty(reply):
    src, _, _ = fubon_setup([reply])
    store = HistStore(FakeClient())
    fubon_source.ingest_kbars(src, store, products=["TXF"], since=FRI, until=FRI, force=False)
    assert store.client.marks[("fubon", "TXF", "kbars", FRI)] == "FAILED"


def ingest_snapshot(kind, src, store, *, force=False, since=FRI):
    if kind == "ticks":
        return run_ticks(src, store, since=since, force=force)
    return shioaji_source.ingest_kbars(
        src, store, products=["TXF"], since=since, until=FRI, min_remaining_bytes=50 * MB, force=force
    )


def shorten_reply(src, kind, count):
    original = getattr(src.api, kind)

    def reply(**kwargs):
        raw = vars(original(**kwargs))
        return {key: values[:count] for key, values in raw.items()}

    setattr(src.api, kind, reply)


@pytest.mark.parametrize("kind", ["ticks", "kbars"])
@pytest.mark.parametrize("count", [0, 1])
def test_snapshot_force_refetch_removes_absent_keys_and_preserves_other_slices(kind, count):
    src, store, client = setup()
    if kind == "kbars":
        src.api.kbars = lambda **kwargs: SimpleNamespace(
            ts=[local_ns(FRI, "09:00") + i * 60 * NS for i in range(3)],
            Open=[1] * 3,
            High=[1] * 3,
            Low=[1] * 3,
            Close=[1] * 3,
            Volume=[1] * 3,
        )
    ingest_snapshot(kind, src, store, since=THU)
    table = "hft.hist_" + kind
    old = client.final(table)
    target = old[-1]
    other = [target._replace(source="other"), target._replace(product="MXF")]
    if kind == "kbars":
        other.append(target._replace(timeframe_min=5))
    store.insert(other)
    shorten_reply(src, kind, count)
    ingest_snapshot(kind, src, store, force=True)
    current = client.final(table)
    friday = [
        row
        for row in current
        if row.source == "shioaji"
        and row.product == "TXF"
        and (row.trading_day == FRI if kind == "ticks" else row.timeframe_min == 1)
    ]
    assert len(friday) == count
    assert all(row in current for row in other)
    if kind == "ticks":
        assert all(row in current for row in old if row.trading_day == THU)
    assert client.marks[("shioaji", "TXF", kind, FRI)] == ("DONE" if count else "EMPTY")
    assert client.commands
    assert all(settings == {"mutations_sync": 1} for _, _, settings in client.commands)


@pytest.mark.parametrize("kind", ["ticks", "kbars"])
@pytest.mark.parametrize("failure", ["delete", "insert"])
def test_snapshot_write_failure_invalidates_success_and_retry_clears_partial_rows(kind, failure):
    src, store, client = setup()
    if kind == "kbars":
        src.api.kbars = lambda **kwargs: SimpleNamespace(
            ts=[local_ns(FRI, "09:00") + i * 60 * NS for i in range(3)],
            Open=[1] * 3,
            High=[1] * 3,
            Low=[1] * 3,
            Close=[1] * 3,
            Volume=[1] * 3,
        )
    ingest_snapshot(kind, src, store)
    store.batch_rows = 1
    client.fail_delete = failure == "delete"
    client.fail_insert_at = client.data_inserts + 2 if failure == "insert" else None
    stats = ingest_snapshot(kind, src, store, force=True)
    assert stats["FAILED" if kind == "ticks" else "failed"] == 1
    assert client.marks[("shioaji", "TXF", kind, FRI)] == "FAILED"
    if failure == "insert":
        assert len(client.final("hft.hist_" + kind)) == 3
    client.fail_delete, client.fail_insert_at = False, None
    shorten_reply(src, kind, 0)
    ingest_snapshot(kind, src, store)
    assert client.final("hft.hist_" + kind) == []
    assert client.marks[("shioaji", "TXF", kind, FRI)] == "EMPTY"
    for index, event in enumerate(client.events):
        if event[0] == "delete":
            assert client.events[index - 1][0] == "insert"
            assert client.events[index + 1] == ("insert", "hft.hist_ingest_watermark")


@pytest.mark.parametrize("day", [MON, FRI])
def test_kbar_snapshot_replaces_night_boundaries_and_preserves_partial_lookback(day):
    previous = day - dt.timedelta(days=1)
    while previous.weekday() >= 5:
        previous -= dt.timedelta(days=1)
    inside = [candle(previous, "15:00"), candle(day, "00:00"), candle(day, "14:59")]
    if day == MON:
        inside.append(candle(previous + dt.timedelta(days=1), "01:00"))
    outside = [candle(previous, "14:59"), candle(day, "15:00")]
    _, old_rows = fubon_source.convert_candles({"data": outside + inside}, product="TXF", ingest_ts=1)
    # The API includes partial previous-day lookback; it must not overwrite that complete day's rows.
    _, fetched = fubon_source.convert_candles(
        {"data": [candle(previous, "14:59", close="999"), candle(day, "09:00")]},
        product="TXF",
        ingest_ts=2,
    )
    client = FakeClient()
    store = HistStore(client)
    store.insert(old_rows)
    stats = run_kbar_windows(
        store,
        source="fubon",
        product="TXF",
        since=day,
        until=day,
        chunk_days=27,
        force=True,
        fetch=lambda start, end: WindowResult(fetched),
    )
    assert stats["rows"] == 1
    assert set(client.final("hft.hist_kbars")) == {
        *old_rows[:2],
        fetched[1]._replace(ingest_ts=client.final("hft.hist_kbars")[-1].ingest_ts),
    }
    assert client.marks[("fubon", "TXF", "kbars", day)] == "DONE"
    sql, params, _ = client.commands[0]
    assert "bar_ts >= {start:Int64} AND bar_ts < {end:Int64}" in sql
    assert params["start"] == old_rows[2].bar_ts
    assert params["end"] == old_rows[1].bar_ts


@pytest.mark.parametrize("kind", ["ticks", "kbars"])
def test_snapshot_quota_response_keeps_previous_rows_without_deleting(kind):
    src, store, client = setup()
    ingest_snapshot(kind, src, store)
    old = client.final("hft.hist_" + kind)
    before = len(client.commands)
    src.api.remaining = [MB]
    ingest_snapshot(kind, src, store, force=True)
    assert client.final("hft.hist_" + kind) == old
    assert len(client.commands) == before
    assert client.marks[("shioaji", "TXF", kind, FRI)] == "PENDING_QUOTA"


def test_fubon_valid_empty_list_clears_previous_rows_and_marks_empty():
    client = FakeClient()
    store = HistStore(client)
    src, _, _ = fubon_setup([{"data": [candle(FRI, "09:00")]}])
    fubon_source.ingest_kbars(src, store, products=["TXF"], since=FRI, until=FRI, force=False)
    assert client.final("hft.hist_kbars")
    empty, _, _ = fubon_setup([{"data": []}])
    fubon_source.ingest_kbars(empty, store, products=["TXF"], since=FRI, until=FRI, force=True)
    assert client.final("hft.hist_kbars") == []
    assert client.marks[("fubon", "TXF", "kbars", FRI)] == "EMPTY"


@pytest.mark.parametrize("failure_batch", [1, 2])
@pytest.mark.parametrize("kind", ["ticks", "kbars"])
def test_snapshot_insert_failure_preserves_old_keys_and_failed_watermark(kind, failure_batch):
    src, store, client = setup()
    if kind == "kbars":
        src.api.kbars = lambda **kwargs: SimpleNamespace(
            ts=[local_ns(FRI, "09:00") + i * 60 * NS for i in range(3)],
            Open=[1] * 3,
            High=[1] * 3,
            Low=[1] * 3,
            Close=[1] * 3,
            Volume=[1] * 3,
        )
    ingest_snapshot(kind, src, store)
    table = "hft.hist_" + kind
    old = client.final(table)
    store.batch_rows = 1
    client.fail_insert_at = client.data_inserts + failure_batch
    before = len(client.commands)
    shorten_reply(src, kind, 2)
    ingest_snapshot(kind, src, store, force=True)
    key = (lambda r: (r.exch_ts, r.seq)) if kind == "ticks" else (lambda r: r.bar_ts)
    assert {key(r) for r in old} <= {key(r) for r in client.final(table)}
    assert client.marks[("shioaji", "TXF", kind, FRI)] == "FAILED"
    assert len(client.commands) == before


def test_snapshot_stale_delete_follows_every_batch_and_uses_fresh_common_version():
    src, store, client = setup()
    ingest_snapshot("ticks", src, store)
    old_max = max(r.ingest_ts for r in client.final("hft.hist_ticks"))
    store.clock_ns = lambda: 1
    store.batch_rows = 1
    client.events.clear()
    ingest_snapshot("ticks", src, store, force=True)
    assert [e[0] for e in client.events] == ["insert"] * 4 + ["delete", "insert"]
    fresh = client.final("hft.hist_ticks")
    assert {r.ingest_ts for r in fresh} == {old_max + 1}
    assert client.commands[-1][1]["threshold"] == old_max + 1


@pytest.mark.parametrize("include_done", [False, True])
def test_kbar_pending_runs_preserve_interleaved_done_rows_and_watermarks(include_done):
    client = FakeClient()
    store = HistStore(client)
    _, original = fubon_source.convert_candles(
        {"data": [candle(TUE, "09:00"), candle(THU, "09:00")]}, product="TXF", ingest_ts=99
    )
    store.insert(original)
    store.mark_days("fubon", "TXF", "kbars", [TUE], "DONE")
    store.mark_days("fubon", "TXF", "kbars", [THU], "EMPTY")
    old_marks = client.inserted("hft.hist_ingest_watermark")[:]
    _, fetched = fubon_source.convert_candles(
        {
            "data": [candle(MON, "09:00"), candle(WED, "09:00"), candle(FRI, "09:00")]
            + ([candle(TUE, "09:00", close="999")] if include_done else [])
        },
        product="TXF",
        ingest_ts=1,
    )
    calls = []
    stats = run_kbar_windows(
        store,
        source="fubon",
        product="TXF",
        since=MON,
        until=FRI,
        chunk_days=27,
        force=False,
        fetch=lambda start, end: calls.append((start, end)) or WindowResult(fetched),
    )
    assert stats["rows"] == 3
    assert len(calls) == 1
    assert set(original) <= set(client.final("hft.hist_kbars"))
    assert [r for r in client.inserted("hft.hist_ingest_watermark") if r.trading_day in (TUE, THU)] == old_marks


def test_ticks_pending_days_preserve_done_data_and_watermark():
    src, store, client = setup()
    run_ticks(src, store, since=WED, until=WED)
    before = client.final("hft.hist_ticks")[:]
    marks = client.inserted("hft.hist_ingest_watermark")[:]
    run_ticks(src, store, since=TUE, until=THU)
    assert set(before) <= set(client.final("hft.hist_ticks"))
    assert [r for r in client.inserted("hft.hist_ingest_watermark") if r.trading_day == WED] == marks
    assert src.api.requested_days == [WED, THU, TUE]


def test_cli_busy_lock_returns_nonzero_before_connect_or_writes(monkeypatch):
    holder, contender = HistStore(FakeClient()), HistStore(FakeClient())
    monkeypatch.setattr(cli.HistStore, "from_env", lambda dry_run=False: contender)
    monkeypatch.setattr(cli.shioaji_source.ShioajiSource, "connect", lambda: pytest.fail("broker connected"))
    with holder.writer_lock("shioaji", "TXF", "ticks"):
        code = cli.main(
            ["ticks", "--products", "TXF", "--env-file", "/nonexistent"],
            now=dt.datetime(2026, 11, 20, 15, 30, tzinfo=TAIPEI),
        )
    assert code == 1
    assert contender.client.calls == []
    assert contender.client.commands == []
    assert contender.client.queries == []


@pytest.mark.parametrize("kind", ["ticks", "kbars"])
def test_cli_nested_ingest_replace_reenters_and_releases_outermost_lock(monkeypatch, kind):
    src, store, client = setup()
    src.close = lambda: None
    monkeypatch.setattr(cli.HistStore, "from_env", lambda dry_run=False: store)
    monkeypatch.setattr(cli.shioaji_source.ShioajiSource, "connect", lambda: src)
    real_flock, operations = store_module.fcntl.flock, []

    def flock(fd, operation):
        operations.append(operation)
        return real_flock(fd, operation)

    monkeypatch.setattr(store_module.fcntl, "flock", flock)
    assert (
        cli.main(
            [kind, "--products", "TXF", "--since", str(FRI), "--env-file", "/nonexistent"]
            + (["--source", "shioaji"] if kind == "kbars" else []),
            now=dt.datetime(2026, 11, 20, 15, 30, tzinfo=TAIPEI),
        )
        == 0
    )
    assert operations == [store_module.fcntl.LOCK_EX | store_module.fcntl.LOCK_NB, store_module.fcntl.LOCK_UN]
    assert store._held == {}
    assert client.marks[("shioaji", "TXF", kind, FRI)] == "DONE"
    with HistStore(client).writer_lock("shioaji", "TXF", kind):
        assert len(operations) == 3


def test_writer_lock_excludes_other_thread_store_and_process_then_releases():
    import concurrent.futures
    import subprocess
    import sys

    store = HistStore(FakeClient())

    def contender():
        with pytest.raises(WriterBusyError):
            with store.writer_lock("shioaji", "TXF", "ticks"):
                pytest.fail("other thread acquired lock")
        return True

    with store.writer_lock("shioaji", "TXF", "ticks"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(contender).result(timeout=5)
        with pytest.raises(WriterBusyError):
            with HistStore(FakeClient()).writer_lock("shioaji", "TXF", "ticks"):
                pytest.fail("other store acquired lock")
        script = """
from types import SimpleNamespace
from research.data_pipeline.hist_ingest.store import HistStore, WriterBusyError
try:
    with HistStore(SimpleNamespace()).writer_lock('shioaji', 'TXF', 'ticks'):
        raise SystemExit(2)
except WriterBusyError:
    raise SystemExit(0)
"""
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr.decode()
    with HistStore(FakeClient()).writer_lock("shioaji", "TXF", "ticks"):
        assert store._held == {}


def test_writer_lock_scope_uses_actual_endpoint_database_and_product(monkeypatch):
    a = HistStore(SimpleNamespace(url="http://127.0.0.1:8123", database="ignored"))
    b = HistStore(SimpleNamespace(host="localhost", port="8123"))
    key = a._lock_key("shioaji", "TXF", "ticks")
    assert key == b._lock_key("shioaji", "TXF", "ticks")
    assert key != b._lock_key("shioaji", "MXF", "ticks")
    assert key != b._lock_key("fubon", "TXF", "ticks")
    assert key != b._lock_key("shioaji", "TXF", "kbars")
    assert key != HistStore(SimpleNamespace(host="localhost", port=8124))._lock_key("shioaji", "TXF", "ticks")
    monkeypatch.setitem(store_module.TABLES, store_module.TickRow, ("scratch.hist_ticks", ()))
    assert key != b._lock_key("shioaji", "TXF", "ticks")


def test_default_lock_directory_is_canonical_repository_log():
    import subprocess
    from pathlib import Path

    common = subprocess.check_output(["git", "rev-parse", "--git-common-dir"], text=True).strip()
    assert store_module.default_lock_directory() == Path(common).resolve().parent / "log" / "hist_ingest" / "locks"
