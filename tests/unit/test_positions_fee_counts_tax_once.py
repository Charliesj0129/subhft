"""A fill's ``fee`` already contains its ``tax``; a position must count the tax once.

``FeeCalculator.compute`` returns ``total = commission + tax`` and ``ExecutionNormalizer.normalize_fill``
puts that total in ``FillEvent.fee`` and the tax portion alone in ``FillEvent.tax`` (the ClickHouse fills
row says so too: ``tax_scaled`` is "tax portion of fee"). ``PositionStore`` used to add ``fee + tax``, so
every taxed fill (a futures sell) counted its tax twice in ``fees_scaled``.
"""

from __future__ import annotations

import pytest

from hft_platform.contracts.execution import FillEvent, Side
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.execution.positions import PositionStore
from hft_platform.tca.fee_calculator import FeeCalculator

_COMMISSION = 600_000  # 60 NTD, x10000
_TAX = 7_000_000  # 700 NTD, x10000
_TOTAL = _COMMISSION + _TAX  # what FeeCalculator puts in FillEvent.fee

_FUTURES_FEES = """\
futures:
  TX:
    commission_per_contract: 60
    tax_rate_bps: 2.0
    tax_side: sell
    tick_size: 1
    point_value: 200
symbol_map:
  TXFL5: TX
"""


def _store(backend: str) -> PositionStore:
    store = PositionStore()
    if backend == "python":
        store._rust_tracker = None
    else:
        if store._rust_tracker is None:
            pytest.skip("rust_core.RustPositionTracker not available")
    return store


def _fill(side: Side, fee: int, tax: int, fill_id: str, price: int = 175_000_000) -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        account_id="ACC",
        order_id=f"O-{fill_id}",
        strategy_id="S1",
        symbol="TXFL5",
        side=side,
        qty=1,
        price=price,
        fee=fee,
        tax=tax,
        ingest_ts_ns=1,
        match_ts_ns=1,
    )


@pytest.mark.parametrize("backend", ["python", "rust"])
def test_fees_scaled_counts_tax_once(backend: str) -> None:
    store = _store(backend)

    store.on_fill(_fill(Side.SELL, fee=_TOTAL, tax=_TAX, fill_id="F1"))

    assert store.positions["ACC:S1:TXFL5"].fees_scaled == _TOTAL


@pytest.mark.parametrize("backend", ["python", "rust"])
def test_fees_scaled_of_a_round_trip_is_the_sum_of_the_fills_fees(backend: str) -> None:
    store = _store(backend)

    store.on_fill(_fill(Side.BUY, fee=_COMMISSION, tax=0, fill_id="F1"))  # a buy is not taxed
    store.on_fill(_fill(Side.SELL, fee=_TOTAL, tax=_TAX, fill_id="F2", price=175_010_000))

    assert store.positions["ACC:S1:TXFL5"].fees_scaled == _COMMISSION + _TOTAL


def test_python_and_rust_stores_report_the_same_fees() -> None:
    py, rust = _store("python"), _store("rust")
    fills = [
        _fill(Side.BUY, fee=_COMMISSION, tax=0, fill_id="F1"),
        _fill(Side.SELL, fee=_TOTAL, tax=_TAX, fill_id="F2"),
        _fill(Side.SELL, fee=_TOTAL, tax=_TAX, fill_id="F3"),
    ]

    for fill in fills:
        py.on_fill(fill)
        rust.on_fill(fill)

    assert py.positions["ACC:S1:TXFL5"].fees_scaled == rust.positions["ACC:S1:TXFL5"].fees_scaled
    assert py.positions["ACC:S1:TXFL5"].fees_scaled == _COMMISSION + 2 * _TOTAL


@pytest.mark.parametrize("backend", ["python", "rust"])
def test_a_normalized_fill_reaches_the_position_with_the_calculators_total(backend: str, tmp_path, monkeypatch) -> None:
    """The producer side of the contract: ``fee`` is the calculator's total, ``tax`` its tax part."""
    symbols = tmp_path / "symbols.yaml"
    symbols.write_text("symbols:\n  - code: 'TXFL5'\n    exchange: 'TAIFEX'\n    price_scale: 10000\n")
    monkeypatch.setenv("SYMBOLS_CONFIG", str(symbols))
    fees = tmp_path / "futures.yaml"
    fees.write_text(_FUTURES_FEES)
    calculator = FeeCalculator.from_yaml(str(fees))
    expected = calculator.compute("TXFL5", "SELL", 1, 175_000_000)
    assert 0 < expected.tax < expected.total  # the fixture must exercise a taxed fill

    normalizer = ExecutionNormalizer(fee_calculator=calculator)
    fill = normalizer.normalize_fill(
        RawExecEvent(
            "deal",
            {
                "seq_no": "D1",
                "ord_no": "O1",
                "code": "TXFL5",
                "action": "Sell",
                "quantity": 1,
                "price": 17_500,
                "ts": 1,
                "account_id": "ACC",
            },
            1,
        )
    )
    assert fill is not None
    assert (fill.fee, fill.tax) == (expected.total, expected.tax)

    store = _store(backend)
    store.on_fill(fill)

    assert store.positions[f"ACC:{fill.strategy_id}:TXFL5"].fees_scaled == expected.total
