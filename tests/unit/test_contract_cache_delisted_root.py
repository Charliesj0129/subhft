"""A stock-future root leaving the listing is a delisting, not a partial download.

THESHOW 2026-11-05: QBF (Ennostar, capital reduction halting the underlying) left
the broker's listing at 08:03. The guard read "a root that still trades is
missing" as a lost download and refused 44 rewrites in a row, freezing the
cache (no new strikes or weeklies) for every other contract.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
import structlog.testing

from hft_platform.config._symbols_contracts import (
    ContractCacheRegressionError,
    contract_cache_dropped_roots,
    contract_cache_regressions,
    write_contract_cache,
)

AS_OF = dt.date(2026, 11, 5)


def _fut(code: str, root: str, delivery: str = "2026/11/18") -> dict:
    return {"code": code, "root": root, "type": "future", "exchange": "TAIFEX", "delivery_date": delivery}


INDEX = [_fut("TXFK6", "TXF"), _fut("MXFK6", "MXF"), _fut("TMFK6", "TMF"), _fut("TMFL6", "TMF", "2026/12/16")]
# Twenty stock-future roots, two rows each, like the real ~250.
STOCKS = [_fut(f"Q{i:02d}K6", f"Q{i:02d}") for i in range(20)] + [
    _fut(f"Q{i:02d}L6", f"Q{i:02d}", "2026/12/16") for i in range(20)
]
QBF = [_fut("QBFK6", "QBF"), _fut("QBFL6", "QBF", "2026/12/16")]


def test_one_stock_future_root_delisted_is_not_a_regression():
    assert contract_cache_regressions(INDEX + STOCKS + QBF, INDEX + STOCKS, as_of=AS_OF) == []
    assert contract_cache_dropped_roots(INDEX + STOCKS + QBF, INDEX + STOCKS, as_of=AS_OF)["future"] == ["QBF"]


def test_a_configured_symbols_root_is_protected():
    reasons = contract_cache_regressions(INDEX + STOCKS + QBF, INDEX + STOCKS, as_of=AS_OF, protected_codes=["QBFK6"])
    assert reasons == ["future roots missing: QBF"]


def test_an_index_root_missing_is_still_refused_nov_02():
    reasons = contract_cache_regressions(INDEX + STOCKS, INDEX[2:] + STOCKS, as_of=AS_OF)
    assert "future roots missing: MXF, TXF" in reasons


def test_many_stock_roots_missing_at_once_is_a_partial_download():
    after = INDEX + [row for row in STOCKS if row["root"] >= "Q04"]  # four roots gone, limit is 3
    reasons = contract_cache_regressions(INDEX + STOCKS, after, as_of=AS_OF)
    assert reasons and reasons[0].startswith("future roots missing: 4 > 3")


def test_zero_futures_is_still_refused_nov_04():
    reasons = contract_cache_regressions(INDEX + STOCKS, [], as_of=AS_OF)
    assert any(r.startswith("future roots missing") for r in reasons)
    assert any(r.startswith("future rows") for r in reasons)


def test_write_accepts_a_delisting_and_logs_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from hft_platform.core import timebase

    monkeypatch.setattr(timebase, "now_ns", lambda: int(dt.datetime(2026, 11, 5, 9).timestamp() * 1e9))
    path = tmp_path / "contracts.json"
    path.write_text(json.dumps({"cache_version": 2393, "contracts": INDEX + STOCKS + QBF}), encoding="utf-8")

    with structlog.testing.capture_logs() as logs:
        write_contract_cache(INDEX + STOCKS, str(path), protected_codes=["TMFK6"])

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["cache_version"] == 2394
    delisted = [e for e in logs if e.get("event") == "contract_roots_delisted"]
    assert delisted and delisted[0]["roots"] == {"future": ["QBF"]}


def test_write_refuses_losing_a_configured_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from hft_platform.core import timebase

    monkeypatch.setattr(timebase, "now_ns", lambda: int(dt.datetime(2026, 11, 5, 9).timestamp() * 1e9))
    path = tmp_path / "contracts.json"
    path.write_text(json.dumps({"cache_version": 7, "contracts": INDEX + STOCKS + QBF}), encoding="utf-8")

    with pytest.raises(ContractCacheRegressionError):
        write_contract_cache(INDEX + STOCKS, str(path), protected_codes=["QBFL6"])
    assert json.loads(path.read_text(encoding="utf-8"))["cache_version"] == 7
