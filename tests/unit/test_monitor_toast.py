"""Tests for toast notification wiring."""

from hft_platform.monitor._types import Toast


def test_toast_fields():
    t = Toast(message="test", style="green", expire_ns=1000)
    assert t.message == "test"
    assert t.expire_ns == 1000


def test_engine_toast_none_by_default():
    from hft_platform.monitor._engine import MonitorEngine
    from hft_platform.monitor._types import MonitorConfig, WatchlistSymbol

    ws = WatchlistSymbol(code="T", name="t", product_type="stock")
    engine = MonitorEngine(MonitorConfig(symbols=(ws,)))
    assert engine._toast is None


def test_sort_cycle_sets_toast():
    from hft_platform.monitor._engine import MonitorEngine
    from hft_platform.monitor._types import MonitorConfig, WatchlistSymbol

    ws = WatchlistSymbol(code="T", name="t", product_type="stock")
    engine = MonitorEngine(MonitorConfig(symbols=(ws,)))
    engine.cycle_sort_mode()
    assert engine._toast is not None
    assert "Sort" in engine._toast.message


def test_toggle_pause_sets_toast():
    from hft_platform.monitor._engine import MonitorEngine
    from hft_platform.monitor._types import MonitorConfig, WatchlistSymbol

    ws = WatchlistSymbol(code="T", name="t", product_type="stock")
    engine = MonitorEngine(MonitorConfig(symbols=(ws,)))
    engine.toggle_pause()
    assert engine._toast is not None
    assert "Paused" in engine._toast.message


def test_toggle_closed_collapse_sets_toast():
    from hft_platform.monitor._engine import MonitorEngine
    from hft_platform.monitor._types import MonitorConfig, WatchlistSymbol

    ws = WatchlistSymbol(code="T", name="t", product_type="stock")
    engine = MonitorEngine(MonitorConfig(symbols=(ws,)))
    engine.toggle_closed_collapse()
    assert engine._toast is not None
    assert "Closed" in engine._toast.message
