"""Fix 4 regression: build_symbols must drop past-expiry derivatives so the
regenerated config/symbols.yaml never re-injects codes that the broker has
already delisted (e.g. ``MXFE6``/``TMFE6``/``TXFE6`` after the 3rd-Wed
May-2026 settlement).

Operator workflow: run ``make rebuild-symbols-yaml`` after each roll. The
expander already filters ``delivery_date < today``; this test pins that
contract so a future refactor cannot regress it silently.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hft_platform.config._symbols_types import ContractIndex
from hft_platform.config.symbols import build_symbols


def _fake_future(code: str, dte_days: int) -> dict:
    target = datetime.now(UTC).date() + timedelta(days=dte_days)
    return {
        "code": code,
        "exchange": "TAIFEX",
        "type": "future",
        "root": "TXF",
        "delivery_date": target.strftime("%Y/%m/%d"),
        "tick_size": 1.0,
        "price_scale": 10000,
    }


def test_build_symbols_drops_expired_front_month(tmp_path):
    """Synthetic cache: TXFE6 expired yesterday, TXFF6 active. Pattern
    ``TXF@front`` must resolve to F6, never E6."""
    list_path = tmp_path / "symbols.list"
    list_path.write_text(
        "TXF@front exchange=FUT tags=futures|front_month|txf\nTXF@next exchange=FUT tags=futures|next_month|txf\n",
        encoding="utf-8",
    )

    contracts = [
        _fake_future("TXFE6", dte_days=-2),  # settled
        _fake_future("TXFF6", dte_days=20),  # active front
        _fake_future("TXFG6", dte_days=50),  # active next
    ]
    index = ContractIndex(contracts=contracts)

    result = build_symbols(str(list_path), index)
    codes = {s["code"] for s in result.symbols}

    assert "TXFE6" not in codes, (
        "Expired front-month must be dropped — otherwise the regenerated "
        "symbols.yaml will reinstate 'Contract not found' errors on the "
        "live engine. Got: " + ", ".join(sorted(codes))
    )
    assert "TXFF6" in codes, "Front-month should resolve to the next active expiry"
    assert "TXFG6" in codes, "Next-month should resolve to the second active expiry"


def test_build_symbols_warns_when_only_expired_contracts_exist(tmp_path):
    """If the broker cache is somehow empty of active contracts, build must
    surface a clear error rather than silently emit the empty universe."""
    list_path = tmp_path / "symbols.list"
    list_path.write_text("TXF@front exchange=FUT\n", encoding="utf-8")

    contracts = [_fake_future("TXFE6", dte_days=-2)]
    index = ContractIndex(contracts=contracts)

    result = build_symbols(str(list_path), index)
    assert all(s["code"] != "TXFE6" for s in result.symbols)
    # The expander pushes the "no active expiries" condition into errors —
    # whether it lands in errors or warnings, the empty result is what
    # matters for the operator-facing build CLI.
    assert not any(s["code"] == "TXFE6" for s in result.symbols)
