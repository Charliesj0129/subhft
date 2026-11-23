from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from scripts import backfill_historical_ticks as bf


def _fake_time(now: list[float], sleeps: list[float]) -> SimpleNamespace:
    return SimpleNamespace(monotonic=lambda: now[0], sleep=sleeps.append)


class _Api:
    def __init__(self, ticks: Any) -> None:
        self._ticks = ticks

    def ticks(self, **_kwargs: Any) -> Any:
        return self._ticks


def _backfill(monkeypatch: pytest.MonkeyPatch, closes: list[float]) -> list[dict[str, Any]]:
    inserted: list[dict[str, Any]] = []
    monkeypatch.setattr(bf, "ch_insert", lambda rows: inserted.extend(dict(r) for r in rows) or len(rows))
    monkeypatch.setattr(bf, "_throttle", lambda: None)
    ticks = SimpleNamespace(
        ts=[1_700_000_000_000_000_000 + i for i in range(len(closes))],
        close=closes,
        volume=[1] * len(closes),
        tick_type=[1] * len(closes),
    )
    n = bf.fetch_and_insert(_Api(ticks), object(), "TXFD6", "FUT", "future", "2026-04-06")
    assert n == len(closes)
    return inserted


@pytest.mark.parametrize(
    ("price", "expected_scaled"),
    [
        (2.01, 2_010_000),
        (4.1, 4_100_000),
        (8.03, 8_030_000),
        (22345.35, 22_345_350_000),
    ],
)
def test_backfill_rounds_price_to_the_exact_scaled_int_when_the_float_product_is_inexact(
    monkeypatch: pytest.MonkeyPatch, price: float, expected_scaled: int
) -> None:
    rows = _backfill(monkeypatch, [price])

    assert rows[0]["price_scaled"] == expected_scaled
    assert isinstance(rows[0]["price_scaled"], int)


def test_backfill_price_scale_is_the_clickhouse_x1e6_scale() -> None:
    assert bf.PRICE_SCALE == 1_000_000


def test_throttle_uses_the_official_window_of_50_queries_per_10_seconds() -> None:
    assert bf.RATE_LIMIT == 50
    assert bf.RATE_WINDOW == pytest.approx(10.0)


def test_throttle_waits_when_50_requests_were_made_less_than_10_seconds_ago(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    sleeps: list[float] = []
    monkeypatch.setattr(bf, "time", _fake_time(now, sleeps))
    monkeypatch.setattr(bf, "_req_times", [])
    for _ in range(bf.RATE_LIMIT):
        bf._throttle()
    assert sleeps == []

    now[0] = 5.5
    bf._throttle()

    assert sleeps == [pytest.approx(10.0 - 5.5 + 0.1)]


def test_throttle_does_not_wait_when_the_oldest_request_left_the_10_second_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    sleeps: list[float] = []
    monkeypatch.setattr(bf, "time", _fake_time(now, sleeps))
    monkeypatch.setattr(bf, "_req_times", [])
    for _ in range(bf.RATE_LIMIT):
        bf._throttle()

    now[0] = 10.5
    bf._throttle()

    assert sleeps == []
