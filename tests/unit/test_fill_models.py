"""Tests for QueueDepletionFill model."""

from research.backtest.fill_models import QueueDepletionFill


def test_post_quote_calculates_queue_position():
    fm = QueueDepletionFill(queue_fraction=0.5)
    pos = fm.post_quote(side="buy", price=100_000_000, book_qty=200)
    assert pos.side == "buy"
    assert pos.price == 100_000_000
    assert pos.queue_ahead == 100


def test_post_quote_full_queue():
    fm = QueueDepletionFill(queue_fraction=1.0)
    pos = fm.post_quote(side="sell", price=101_000_000, book_qty=50)
    assert pos.queue_ahead == 50


def test_check_fills_no_fill_when_queue_remains():
    fm = QueueDepletionFill(queue_fraction=0.5)
    pos = fm.post_quote(side="buy", price=100_000_000, book_qty=200)
    fills = fm.check_fills([pos], trade_price=100_000_000, trade_volume=50)
    assert len(fills) == 0
    assert pos.queue_ahead == 50


def test_check_fills_fill_when_queue_depleted():
    fm = QueueDepletionFill(queue_fraction=0.5)
    pos = fm.post_quote(side="buy", price=100_000_000, book_qty=20)
    fills = fm.check_fills([pos], trade_price=100_000_000, trade_volume=15)
    assert len(fills) == 1
    assert fills[0].side == "buy"
    assert fills[0].price == 100_000_000


def test_check_fills_ignores_wrong_price():
    fm = QueueDepletionFill(queue_fraction=0.5)
    pos = fm.post_quote(side="buy", price=100_000_000, book_qty=20)
    fills = fm.check_fills([pos], trade_price=101_000_000, trade_volume=100)
    assert len(fills) == 0
    assert pos.queue_ahead == 10


def test_sell_fill_on_ask_trade():
    fm = QueueDepletionFill(queue_fraction=0.5)
    pos = fm.post_quote(side="sell", price=101_000_000, book_qty=10)
    fills = fm.check_fills([pos], trade_price=101_000_000, trade_volume=10)
    assert len(fills) == 1
    assert fills[0].side == "sell"


def test_fill_model_label():
    fm = QueueDepletionFill(queue_fraction=0.5)
    assert fm.label == "QueueDepletion(qf=0.5)"
