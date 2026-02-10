import json
import time
from pathlib import Path
from unittest.mock import patch

from hft_platform.feed_adapter.subscription_state import SubscriptionStateManager


def test_add_remove_and_clear_symbols(tmp_path: Path) -> None:
    SubscriptionStateManager.reset_for_tests()
    state_path = tmp_path / "subscriptions.json"
    manager = SubscriptionStateManager(state_path=str(state_path))

    assert manager.symbol_count == 0
    manager.add_symbol("2330", "TSE", "stock")
    manager.add_symbol("TXFD6", "TAIFEX", "futures")
    assert manager.symbol_count == 2

    symbols = sorted(manager.get_symbols(), key=lambda item: item["code"])
    assert symbols == [
        {"code": "2330", "exchange": "TSE", "product_type": "stock"},
        {"code": "TXFD6", "exchange": "TAIFEX", "product_type": "futures"},
    ]

    manager.remove_symbol("2330", "TSE")
    assert manager.symbol_count == 1

    manager.clear()
    assert manager.symbol_count == 0


def test_record_tick_and_stale_detection(tmp_path: Path) -> None:
    SubscriptionStateManager.reset_for_tests()
    state_path = tmp_path / "subscriptions.json"
    manager = SubscriptionStateManager(state_path=str(state_path))

    manager.add_symbol("2330", "TSE", "stock")
    with patch("hft_platform.feed_adapter.subscription_state.timebase.now_ns", return_value=1_000_000_000):
        manager.record_tick("2330", "TSE", tick_ts_ns=500_000_000)
        state = manager.get_symbol_state("2330", "TSE")
        assert state is not None
        assert state.tick_count == 1

        stale = manager.get_stale_symbols(max_gap_s=0.1)
        assert "TSE:2330" in stale
        fresh = manager.get_stale_symbols(max_gap_s=1.0)
        assert "TSE:2330" not in fresh


def test_save_load_roundtrip(tmp_path: Path) -> None:
    SubscriptionStateManager.reset_for_tests()
    state_path = tmp_path / "subscriptions.json"
    manager = SubscriptionStateManager(state_path=str(state_path))
    manager.add_symbol("2330", "TSE", "stock")
    assert manager.save(force=True)

    reloaded = SubscriptionStateManager(state_path=str(state_path))
    assert reloaded.load() is True
    assert reloaded.symbol_count == 1


def test_load_invalid_json(tmp_path: Path) -> None:
    SubscriptionStateManager.reset_for_tests()
    state_path = tmp_path / "subscriptions.json"
    state_path.write_text("{bad json")
    manager = SubscriptionStateManager(state_path=str(state_path))
    assert manager.load() is False


def test_load_missing_file(tmp_path: Path) -> None:
    SubscriptionStateManager.reset_for_tests()
    state_path = tmp_path / "subscriptions.json"
    manager = SubscriptionStateManager(state_path=str(state_path))
    assert manager.load() is False


def test_save_when_clean_returns_true(tmp_path: Path) -> None:
    SubscriptionStateManager.reset_for_tests()
    state_path = tmp_path / "subscriptions.json"
    manager = SubscriptionStateManager(state_path=str(state_path))
    assert manager.save() is True


def test_auto_save_thread_writes_file(tmp_path: Path) -> None:
    SubscriptionStateManager.reset_for_tests()
    state_path = tmp_path / "subscriptions.json"
    manager = SubscriptionStateManager(state_path=str(state_path))
    manager.add_symbol("2330", "TSE", "stock")
    manager._auto_save_interval_s = 0.01
    manager.start_auto_save()
    time.sleep(0.05)
    manager.stop()
    assert state_path.exists()


def test_singleton_get_and_reset(tmp_path: Path) -> None:
    SubscriptionStateManager.reset_for_tests()
    state_path = tmp_path / "subscriptions.json"
    manager_1 = SubscriptionStateManager.get(state_path=str(state_path))
    manager_2 = SubscriptionStateManager.get()
    assert manager_1 is manager_2
    SubscriptionStateManager.reset_for_tests()
    manager_3 = SubscriptionStateManager.get(state_path=str(state_path))
    assert manager_3 is not manager_1
