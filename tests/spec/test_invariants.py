import pytest

from hft_platform.contracts.execution import FillEvent, Side
from hft_platform.events import BidAskEvent, MetaData
from hft_platform.execution.positions import PositionStore
from hft_platform.feed_adapter.lob_engine import LOBEngine

try:
    from hypothesis import given
    from hypothesis import strategies as st

    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False

# Mock st for Fallback to run basic check if hypothesis missing
if not HYPOTHESIS_AVAILABLE:
    # Minimal stub to allow file to be parsed and run a single case
    def given(*args, **kwargs):
        def decorator(f):
            def wrapper():
                pytest.skip("hypothesis not installed")

            return wrapper

        return decorator

    class strategies:
        def lists(self, *args, **kwargs):
            return None

        def tuples(self, *args, **kwargs):
            return None

        def integers(self, *args, **kwargs):
            return None

        def text(self, *args, **kwargs):
            return None

    st = strategies()


def _build_levels(base: int, steps: list[int], vols: list[int], descending: bool) -> list[list[int]]:
    prices = []
    current = base
    for step in steps:
        prices.append(current)
        current = current - step if descending else current + step
    return [[price, vol] for price, vol in zip(prices, vols)]


@given(
    st.lists(
        st.tuples(
            st.text(min_size=4, max_size=4),
            st.integers(min_value=-100, max_value=100),
            st.integers(min_value=1, max_value=1000),
        )
    )
)
def test_position_conservation(ops):
    """
    Invariant: Position must be exactly equal to sum of signed fills.
    Ops: List of (symbol, qty_delta, price)
    """
    store = PositionStore()

    # Track expected per symbol
    expected = {}

    for symbol, delta, price in ops:
        if delta == 0:
            continue

        # Action
        side = Side.BUY if delta > 0 else Side.SELL
        qty = abs(delta)

        # Construct FillEvent
        # Need match_ts_ns, etc.
        import time

        fill = FillEvent(
            fill_id="e1",
            account_id="a1",
            order_id="o1",
            strategy_id="s1",
            symbol=symbol,
            side=side,
            qty=qty,
            price=int(price),  # Fixed point?
            fee=0,
            tax=0,
            ingest_ts_ns=time.time_ns(),
            match_ts_ns=time.time_ns(),
        )
        store.on_fill(fill)

        # Oracle logic
        expected[symbol] = expected.get(symbol, 0) + delta

    # Assert
    # PositionStore stores positions in dict: key = acc:strat:sym
    for sym, val in expected.items():
        key = f"a1:s1:{sym}"
        if key in store.positions:
            assert store.positions[key].net_qty == val
        else:
            assert val == 0


def test_manual_invariant_check():
    """Manual fallback if hypothesis missing."""
    store = PositionStore()
    ops = [("2330", 1, 10), ("2330", -1, 10), ("2317", 5, 50)]

    for symbol, delta, price in ops:
        side = Side.BUY if delta > 0 else Side.SELL
        import time

        fill = FillEvent(
            "ex", "a1", "oid", "s1", symbol, side, abs(delta), int(price), 0, 0, time.time_ns(), time.time_ns()
        )
        store.on_fill(fill)

    key_2330 = "a1:s1:2330"
    if key_2330 in store.positions:
        assert store.positions[key_2330].net_qty == 0

    key_2317 = "a1:s1:2317"
    assert store.positions[key_2317].net_qty == 5


@given(
    st.integers(min_value=100, max_value=100000),
    st.integers(min_value=1, max_value=1000),
    st.lists(st.integers(min_value=1, max_value=50), min_size=1, max_size=5),
    st.lists(st.integers(min_value=1, max_value=50), min_size=1, max_size=5),
)
def test_lob_stats_invariants(base_price, spread, bid_steps, ask_steps):
    if not HYPOTHESIS_AVAILABLE:
        # Fallback to a deterministic example.
        base_price = 1000
        spread = 5
        bid_steps = [1, 1]
        ask_steps = [1, 2]

    bid_vols = [10 for _ in bid_steps]
    ask_vols = [12 for _ in ask_steps]

    bids = _build_levels(base_price, bid_steps, bid_vols, descending=True)
    asks = _build_levels(base_price + spread, ask_steps, ask_vols, descending=False)

    engine = LOBEngine()
    meta = MetaData(seq=1, topic="bidask", source_ts=1, local_ts=1)
    event = BidAskEvent(meta=meta, symbol="AAA", bids=bids, asks=asks, is_snapshot=True)
    stats = engine.process_event(event)

    assert stats.best_bid == bids[0][0]
    assert stats.best_ask == asks[0][0]
    assert stats.spread == stats.best_ask - stats.best_bid
    assert stats.mid_price == (stats.best_bid + stats.best_ask) / 2.0
    assert stats.bid_depth == sum(v for _, v in bids)
    assert stats.ask_depth == sum(v for _, v in asks)
