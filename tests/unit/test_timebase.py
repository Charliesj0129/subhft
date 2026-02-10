import datetime as dt
from hft_platform.core import timebase


def test_resolve_tz_fallback_to_utc(monkeypatch):
    monkeypatch.setenv("HFT_TS_TZ", "Invalid/Zone")
    tz_name, tzinfo = timebase._resolve_tz()
    assert tz_name == "Invalid/Zone"
    assert tzinfo == dt.timezone.utc


def test_coerce_ns_int_float_and_datetime():
    naive = dt.datetime(2020, 1, 1, 0, 0, 0)
    expected = int(naive.replace(tzinfo=timebase.TZINFO).timestamp() * 1e9)
    assert timebase.coerce_ns(naive) == expected

    # int paths
    assert timebase.coerce_ns(1_700_000_000) == 1_700_000_000 * 1_000_000_000
    assert timebase.coerce_ns(1_700_000_000_000) == 1_700_000_000_000 * 1_000_000
    assert timebase.coerce_ns(1_700_000_000_000_000) == 1_700_000_000_000_000 * 1_000
    assert timebase.coerce_ns(1_700_000_000_000_000_000) == 1_700_000_000_000_000_000

    # float paths
    assert timebase.coerce_ns(1_700_000_000.5) == int(1_700_000_000.5 * 1e9)
    assert timebase.coerce_ns(1_700_000_000_000.5) == int(1_700_000_000_000.5 * 1e6)
    assert timebase.coerce_ns(1_700_000_000_000_000.5) == int(1_700_000_000_000_000.5 * 1e3)
    assert timebase.coerce_ns(1_700_000_000_000_000_000.5) == int(1_700_000_000_000_000_000.5)


def test_coerce_ns_none_returns_zero():
    assert timebase.coerce_ns(None) == 0


def test_monotonic_and_perf_ns_are_ints():
    assert isinstance(timebase.monotonic_ns(), int)
    assert isinstance(timebase.perf_ns(), int)


def test_coerce_ns_unknown_type_returns_zero():
    assert timebase.coerce_ns("not-a-timestamp") == 0


def test_coerce_ns_timestamp_error_returns_zero():
    class BadTimestamp:
        def timestamp(self):
            raise ValueError("boom")

    assert timebase.coerce_ns(BadTimestamp()) == 0
