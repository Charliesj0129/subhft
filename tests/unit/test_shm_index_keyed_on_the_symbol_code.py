"""The SHM monitor index must be keyed by the code `_publish_to_shm` looks up.

Production shape these are written from (THESHOW, 2026-09-19T15:19:57Z, one
line, at boot, then nothing for the life of the process)::

    {"error": "'ascii' codec can't encode characters in position 27-30:
      ordinal not in range(128)",
     "event": "shm_publisher_init_failed", "level": "warning"}

``client.symbols`` is a ``list[dict]`` loaded from ``symbols.yaml``, and
``_init_shm_publisher`` keyed its table on ``str(sym)`` -- the repr of the whole
mapping. Two things followed, the second hiding the first:

  1. the key was ``"{'code': '2330', 'name': '台積電', ...}"``, which
     ``_publish_to_shm(symbol="2330")`` can never match, while still occupying
     one of ``max_symbols`` slots;
  2. that repr carries the contract's Chinese name, ``_symbol_hash`` encoded it
     as ASCII, and the ``UnicodeEncodeError`` was caught by the ``except`` below
     and disabled the publisher entirely.

The existing coverage tests passed ``["2330", "TXFD6"]`` -- a list of bare
codes, a shape production never produces -- so they exercised neither.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.ipc.shm_snapshot import _symbol_hash
from hft_platform.services.market_data import MarketDataService, _subscription_code


def _service(symbols: object, *, max_symbols: int = 64) -> MarketDataService:
    """A service built for `_init_shm_publisher` alone, via ``__new__``."""
    svc = MarketDataService.__new__(MarketDataService)
    svc.client = SimpleNamespace(symbols=symbols)
    svc._shm_publisher = None
    svc._shm_symbol_index = {}
    svc._shm_symbol_hashes = {}
    writer = MagicMock()
    writer.max_symbols = max_symbols
    with patch("hft_platform.services.market_data.ShmSnapshotWriter", return_value=writer):
        with patch.dict(
            "os.environ",
            {"HFT_MONITOR_SHM_MAX_SYMBOLS": str(max_symbols)},
        ):
            svc._init_shm_publisher()
    return svc


#: The real ``symbols.yaml`` shape: a mapping, with a Chinese display name.
_YAML_SHAPE = [
    {"code": "2330", "name": "台積電", "exchange": "TSE", "product_type": "STK"},
    {"code": "TMFJ6", "name": "小型臺指期貨", "exchange": "TAIFEX", "product_type": "FUT"},
]


class TestTheHashIsTotal:
    def test_a_chinese_name_no_longer_raises(self) -> None:
        """The exact input that killed the publisher at boot."""
        assert isinstance(_symbol_hash("{'code': '2330', 'name': '台積電'}"), int)

    def test_ascii_symbols_hash_exactly_as_before(self) -> None:
        """UTF-8 is ASCII-compatible: existing segments and readers stay valid."""
        for symbol in ("2330", "TMFJ6", "TXO15000C6", ""):
            expected = 0xCBF29CE484222325
            for b in symbol.encode("ascii"):
                expected = ((expected ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
            assert _symbol_hash(symbol) == expected

    def test_the_hash_stays_in_64_bits(self) -> None:
        assert 0 <= _symbol_hash("小型臺指期貨") < 2**64

    def test_different_symbols_hash_differently(self) -> None:
        assert _symbol_hash("TMFJ6") != _symbol_hash("TMFI6")


class TestTheSubscriptionCode:
    def test_a_yaml_mapping_yields_its_code(self) -> None:
        assert _subscription_code({"code": "TMFJ6", "name": "小型臺指期貨"}) == "TMFJ6"

    def test_a_bare_code_is_itself(self) -> None:
        assert _subscription_code("TMFJ6") == "TMFJ6"

    def test_a_contract_object_yields_its_code(self) -> None:
        assert _subscription_code(SimpleNamespace(code="TMFJ6")) == "TMFJ6"

    def test_a_mapping_without_a_code_yields_nothing(self) -> None:
        assert _subscription_code({"name": "台積電"}) == ""

    def test_none_yields_nothing(self) -> None:
        assert _subscription_code(None) == ""


class TestTheIndexSurvivesTheRealSymbolShape:
    def test_the_publisher_is_not_disabled_by_a_chinese_name(self) -> None:
        """The whole point: one boot warning used to cost the entire feed."""
        svc = _service(_YAML_SHAPE)
        assert svc._shm_publisher is not None

    def test_the_index_is_keyed_by_the_code(self) -> None:
        svc = _service(_YAML_SHAPE)
        assert set(svc._shm_symbol_index) == {"2330", "TMFJ6"}
        assert set(svc._shm_symbol_hashes) == {"2330", "TMFJ6"}

    def test_no_dict_repr_ever_reaches_the_table(self) -> None:
        svc = _service(_YAML_SHAPE)
        assert not [k for k in svc._shm_symbol_index if k.startswith("{")]

    def test_the_prebuilt_hash_matches_what_publish_would_compute(self) -> None:
        """A key that matches but a hash that does not is the same outage."""
        svc = _service(_YAML_SHAPE)
        for code, stored in svc._shm_symbol_hashes.items():
            assert stored == _symbol_hash(code)

    def test_slots_are_assigned_from_zero_without_gaps(self) -> None:
        svc = _service(_YAML_SHAPE)
        assert sorted(svc._shm_symbol_index.values()) == [0, 1]

    def test_a_bare_code_list_still_works(self) -> None:
        """Some brokers expose ``subscribed_symbols`` as plain strings."""
        svc = MarketDataService.__new__(MarketDataService)
        svc.client = SimpleNamespace(subscribed_symbols=["2330", "TMFJ6"], symbols=None)
        svc._shm_publisher = None
        svc._shm_symbol_index = {}
        svc._shm_symbol_hashes = {}
        writer = MagicMock()
        writer.max_symbols = 64
        with patch("hft_platform.services.market_data.ShmSnapshotWriter", return_value=writer):
            svc._init_shm_publisher()
        assert set(svc._shm_symbol_index) == {"2330", "TMFJ6"}

    def test_a_duplicated_code_does_not_burn_two_slots(self) -> None:
        svc = _service([{"code": "TMFJ6"}, {"code": "TMFJ6"}, {"code": "2330"}])
        assert svc._shm_symbol_index == {"TMFJ6": 0, "2330": 1}

    def test_an_entry_without_a_code_is_skipped_not_indexed(self) -> None:
        svc = _service([{"name": "台積電"}, {"code": "TMFJ6"}])
        assert svc._shm_symbol_index == {"TMFJ6": 0}

    def test_the_table_stops_at_max_symbols(self) -> None:
        svc = _service([{"code": f"S{i}"} for i in range(10)], max_symbols=4)
        assert len(svc._shm_symbol_index) == 4
        assert sorted(svc._shm_symbol_index.values()) == [0, 1, 2, 3]

    def test_a_publish_finds_the_slot_the_index_built(self) -> None:
        """End to end: the two halves must agree on the key and the hash."""
        svc = _service(_YAML_SHAPE)
        svc._shm_lob_cache = {}
        svc._shm_feat_cache = {}
        stats = SimpleNamespace(
            local_ts=1_700_000_000_000_000_000,
            bid_price=0,
            ask_price=0,
            bid_size=0,
            ask_size=0,
            spread=0,
            mid_price_x2=0,
            imbalance=0.0,
            total_bid_size=0,
            total_ask_size=0,
        )
        svc._publish_to_shm("TMFJ6", stats, None)

        assert svc._shm_symbol_index["TMFJ6"] == 1
        assert svc._shm_publisher.publish.called
        slot_idx, _ts, sym_hash = svc._shm_publisher.publish.call_args.args[:3]
        assert slot_idx == 1
        assert sym_hash == _symbol_hash("TMFJ6")


class TestTheFailurePathIsUnchanged:
    def test_a_writer_that_cannot_be_created_still_disables_cleanly(self) -> None:
        svc = MarketDataService.__new__(MarketDataService)
        svc.client = SimpleNamespace(symbols=_YAML_SHAPE)
        svc._shm_publisher = None
        svc._shm_symbol_index = {}
        svc._shm_symbol_hashes = {}
        with patch(
            "hft_platform.services.market_data.ShmSnapshotWriter",
            side_effect=OSError("shm create failed"),
        ):
            svc._init_shm_publisher()
        assert svc._shm_publisher is None

    def test_a_client_without_any_symbols_indexes_nothing(self) -> None:
        svc = _service(None)
        assert svc._shm_symbol_index == {}
        assert svc._shm_publisher is not None


@pytest.mark.parametrize("symbols", [[], None, ()])
def test_an_empty_universe_is_not_an_error(symbols: object) -> None:
    svc = _service(symbols)
    assert svc._shm_publisher is not None
    assert svc._shm_symbol_index == {}
