from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus, Side
from hft_platform.events import BidAskEvent, MetaData, TickEvent
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.recorder.mapper import map_event_to_record


def _metadata(tmp_path) -> SymbolMetadata:
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: 'AAA'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    return SymbolMetadata(str(cfg))


def test_map_tick_event(tmp_path):
    metadata = _metadata(tmp_path)
    meta = MetaData(seq=1, topic="tick", source_ts=10, local_ts=20)
    event = TickEvent(
        meta=meta,
        symbol="AAA",
        price=123450,
        volume=5,
        total_volume=0,
        bid_side_total_vol=0,
        ask_side_total_vol=0,
        is_simtrade=False,
        is_odd_lot=False,
    )

    topic, row = map_event_to_record(event, metadata)
    assert topic == "market_data"
    assert row["symbol"] == "AAA"
    assert row["exchange"] == "TSE"
    assert row["price"] == 12.345
    assert row["seq_no"] == 1


def test_map_bidask_event(tmp_path):
    metadata = _metadata(tmp_path)
    meta = MetaData(seq=2, topic="bidask", source_ts=11, local_ts=21)
    event = BidAskEvent(meta=meta, symbol="AAA", bids=[[10000, 1]], asks=[[11000, 2]], is_snapshot=True)

    topic, row = map_event_to_record(event, metadata)
    assert topic == "market_data"
    assert row["type"] == "Snapshot"
    assert row["bids_price"] == [1.0]
    assert row["asks_price"] == [1.1]


def test_map_bidask_non_snapshot_dict_levels(tmp_path):
    metadata = _metadata(tmp_path)
    meta = MetaData(seq=3, topic="bidask", source_ts=12, local_ts=22)
    event = BidAskEvent(
        meta=meta,
        symbol="AAA",
        bids=[{"price": 10000, "volume": 1}],
        asks=[{"price": 11000, "volume": 2}],
        is_snapshot=False,
    )

    topic, row = map_event_to_record(event, metadata)
    assert topic == "market_data"
    assert row["type"] == "BidAsk"
    assert row["bids_price"] == [1.0]
    assert row["asks_price"] == [1.1]


def test_map_order_and_fill(tmp_path):
    metadata = _metadata(tmp_path)

    order = OrderEvent(
        order_id="O1",
        strategy_id="S1",
        symbol="AAA",
        status=OrderStatus.SUBMITTED,
        submitted_qty=10,
        filled_qty=0,
        remaining_qty=10,
        price=10000,
        side=Side.BUY,
        ingest_ts_ns=100,
        broker_ts_ns=200,
    )
    topic, row = map_event_to_record(order, metadata)
    assert topic == "orders"
    assert row["price"] == 1.0
    assert row["status"] == "SUBMITTED"

    fill = FillEvent(
        fill_id="F1",
        account_id="A1",
        order_id="O1",
        strategy_id="S1",
        symbol="AAA",
        side=Side.SELL,
        qty=2,
        price=12000,
        fee=100,
        tax=0,
        ingest_ts_ns=100,
        match_ts_ns=110,
    )
    topic, row = map_event_to_record(fill, metadata)
    assert topic == "fills"
    assert row["price"] == 1.2
    assert row["fee"] == 0.01


def test_map_unknown_event_returns_none(tmp_path):
    metadata = _metadata(tmp_path)

    class Dummy:
        pass

    assert map_event_to_record(Dummy(), metadata) is None
