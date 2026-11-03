"""A contract-cache rewrite must not drop futures/options the cache already holds.

THESHOW, 2026-11-02: a refresh wrote a ``contracts.json`` with every TXF and MXF
row missing. The boot universe roll read it, could not resolve ``TXF@front``,
refused, and the pool subscribed the canonical ``symbols.yaml`` (last edited
04-26) for the whole night session. The only check before the write refused a
cache with *zero* derivatives; a cache missing two roots passed it.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import structlog.testing

from hft_platform.config.symbols import (
    ContractCacheRegressionError,
    contract_cache_regressions,
    write_contract_cache,
)


def _fut(code: str, root: str, delivery: str = "2026/11/18") -> dict:
    return {"code": code, "root": root, "type": "future", "exchange": "TAIFEX", "delivery_date": delivery}


def _opt(code: str, root: str = "TXO") -> dict:
    return {"code": code, "root": root, "type": "option", "exchange": "TAIFEX", "delivery_date": "2026/11/18"}


FULL = [_fut("TXFK6", "TXF"), _fut("MXFK6", "MXF"), _fut("TMFK6", "TMF"), _fut("TMFL6", "TMF", "2026/12/16")]
PARTIAL_NOV_02 = [_fut("TMFK6", "TMF"), _fut("TMFL6", "TMF", "2026/12/16")]


def _seed(path: Path, contracts: list[dict]) -> None:
    path.write_text(json.dumps({"cache_version": 3, "contracts": contracts}), encoding="utf-8")


def test_write_refuses_a_rewrite_that_drops_a_futures_root(tmp_path: Path) -> None:
    cache = tmp_path / "contracts.json"
    _seed(cache, FULL)

    with pytest.raises(ContractCacheRegressionError) as err:
        write_contract_cache(PARTIAL_NOV_02, str(cache))

    assert "MXF" in str(err.value) and "TXF" in str(err.value)
    assert json.loads(cache.read_text(encoding="utf-8"))["contracts"] == FULL
    assert not (tmp_path / "contracts.json.tmp").exists()


def test_write_accepts_a_monthly_settlement(tmp_path: Path) -> None:
    """TMFK6 settles and TMFM7 lists: same roots, same row count."""
    cache = tmp_path / "contracts.json"
    _seed(cache, FULL)
    after = [_fut("TXFL6", "TXF"), _fut("MXFL6", "MXF"), _fut("TMFL6", "TMF"), _fut("TMFA7", "TMF", "2027/01/20")]

    write_contract_cache(after, str(cache))

    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["contracts"] == after
    assert data["cache_version"] == 4


def test_write_refuses_a_rewrite_that_halves_the_option_rows(tmp_path: Path) -> None:
    cache = tmp_path / "contracts.json"
    before = FULL + [_opt(f"TXO{k}K6") for k in range(20)]
    _seed(cache, before)
    after = FULL + [_opt(f"TXO{k}K6") for k in range(9)]

    with pytest.raises(ContractCacheRegressionError, match="option rows 20 -> 9"):
        write_contract_cache(after, str(cache))


def test_write_with_allow_regression_replaces_the_cache(tmp_path: Path) -> None:
    cache = tmp_path / "contracts.json"
    _seed(cache, FULL)

    write_contract_cache(PARTIAL_NOV_02, str(cache), allow_regression=True)

    assert json.loads(cache.read_text(encoding="utf-8"))["contracts"] == PARTIAL_NOV_02


def test_regressions_ignore_rows_without_an_explicit_root() -> None:
    """A pre-#698 cache has no root; ``TXFK6`` would derive to ``TXFK``, a new
    "root" every month, and turn each settlement into a false refusal."""
    legacy = [{"code": "TXFK6", "type": "future"}, {"code": "TMFK6", "type": "future"}]
    after = [_fut("TXFL6", "TXF"), _fut("TMFL6", "TMF")]

    assert contract_cache_regressions(legacy, after) == []


def test_write_over_an_unreadable_cache_is_allowed(tmp_path: Path) -> None:
    cache = tmp_path / "contracts.json"
    cache.write_text("{not json", encoding="utf-8")

    write_contract_cache(PARTIAL_NOV_02, str(cache))

    assert json.loads(cache.read_text(encoding="utf-8"))["contracts"] == PARTIAL_NOV_02


def test_refresh_keeps_the_cache_when_the_sdk_walk_lost_two_roots(tmp_path: Path) -> None:
    """The 2026-11-02 shape end to end: SDK walk yields TMF only."""
    cache_path = tmp_path / "contracts.json"
    _seed(cache_path, FULL)

    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

    walked = [
        SimpleNamespace(code="TMFK6", root="TMF", delivery_date="2026/11/18", name="TMF"),
        SimpleNamespace(code="TMFL6", root="TMF", delivery_date="2026/12/16", name="TMF"),
    ]

    def _iter(category):  # noqa: ANN001, ANN202 — test double for the SDK walk
        return walked if category is client.api.Contracts.Futures else []

    with patch.object(ShioajiClient, "__init__", lambda self, *a, **kw: None):
        client = ShioajiClient.__new__(ShioajiClient)
        client._contract_cache_path = str(cache_path)
        client._contract_refresh_status_path = str(tmp_path / "contract_refresh_status.json")
        client._contract_refresh_running = True
        client._contract_refresh_thread = None
        client._contract_refresh_lock = threading.Lock()
        client._contract_refresh_version = 0
        client._contract_refresh_last_diff = {}
        client._contract_refresh_resubscribe_policy = "diff"
        client._contract_refresh_s = 3600.0
        client.api = MagicMock()
        client.metrics = None
        client.config_path = str(tmp_path / "symbols.yaml")
        client.logged_in = True
        client.subscribed_codes = {"TXFK6"}
        client.symbols = [{"code": "TXFK6", "exchange": "TAIFEX"}]
        client._resubscribe_all = MagicMock()
        client.api.Contracts.Stocks.TSE = []
        client.api.Contracts.Stocks.OTC = []

        with (
            patch("hft_platform.feed_adapter.shioaji.contracts_runtime.iter_contract_category", _iter),
            patch.object(client, "_ensure_contracts", return_value=True),
            patch.object(client, "_load_config") as load_config,
            structlog.testing.capture_logs() as logs,
        ):
            client._refresh_contracts_and_symbols()

    assert json.loads(cache_path.read_text(encoding="utf-8"))["contracts"] == FULL
    assert client._resubscribe_all.call_count == 0
    assert load_config.call_count == 0
    assert not client._contract_refresh_lock.locked()
    failed = [entry for entry in logs if entry.get("event") == "contract_refresh_integrity_failed"]
    assert failed and "TXF" in failed[0]["error"]
    status = json.loads((tmp_path / "contract_refresh_status.json").read_text(encoding="utf-8"))
    assert status.get("result") == "error"
