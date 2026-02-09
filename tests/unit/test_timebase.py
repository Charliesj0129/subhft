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
