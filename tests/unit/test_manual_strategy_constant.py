"""Tests for MANUAL_STRATEGY_ID constant and its usage contract."""

from unittest.mock import MagicMock, patch


def test_manual_strategy_id_is_string():
    from hft_platform.contracts.constants import MANUAL_STRATEGY_ID

    assert isinstance(MANUAL_STRATEGY_ID, str)
    assert len(MANUAL_STRATEGY_ID) > 0


def test_manual_strategy_id_is_not_wildcard():
    """MANUAL must NOT be '*' — wildcard matching is the bug we're fixing."""
    from hft_platform.contracts.constants import MANUAL_STRATEGY_ID

    assert MANUAL_STRATEGY_ID != "*"


def test_manual_strategy_id_is_uppercase():
    """Convention: special strategy IDs are uppercase for visibility in logs."""
    from hft_platform.contracts.constants import MANUAL_STRATEGY_ID

    assert MANUAL_STRATEGY_ID == MANUAL_STRATEGY_ID.upper()


def _make_position_store():
    """Create a PositionStore with metrics and metadata mocked."""
    with patch("hft_platform.execution.positions.MetricsRegistry.get", return_value=MagicMock()):
        from hft_platform.execution.positions import Position, PositionStore

        store = PositionStore()
        # Seed two strategy positions for same symbol
        store.positions["acc:alpha:TXFD6"] = Position(
            account_id="acc",
            strategy_id="alpha",
            symbol="TXFD6",
            net_qty=2,
        )
        store.positions["acc:beta:TXFD6"] = Position(
            account_id="acc",
            strategy_id="beta",
            symbol="TXFD6",
            net_qty=-1,
        )
        return store


def test_net_qty_for_symbol_without_filter_includes_recovery():
    """Without strategy_id filter, recovery positions ARE included."""
    store = _make_position_store()
    store._recovery_positions["acc:MANUAL:TXFD6"] = {
        "symbol": "TXFD6",
        "net_qty": 1,
        "strategy_id": "MANUAL",
    }
    # No filter: alpha(+2) + beta(-1) + recovery(+1) = +2
    assert store.net_qty_for_symbol("TXFD6") == 2


def test_net_qty_for_symbol_with_filter_excludes_other_strategy_recovery():
    """With strategy_id='alpha', MANUAL recovery must NOT leak in."""
    store = _make_position_store()
    store._recovery_positions["acc:MANUAL:TXFD6"] = {
        "symbol": "TXFD6",
        "net_qty": 1,
        "strategy_id": "MANUAL",
    }
    # Filter alpha: only alpha's +2
    assert store.net_qty_for_symbol("TXFD6", strategy_id="alpha") == 2


def test_net_qty_for_symbol_manual_filter_returns_only_manual():
    """Querying strategy_id='MANUAL' returns only MANUAL recovery positions."""
    store = _make_position_store()
    store._recovery_positions["acc:MANUAL:TXFD6"] = {
        "symbol": "TXFD6",
        "net_qty": 1,
        "strategy_id": "MANUAL",
    }
    # Filter MANUAL: only the recovery +1
    assert store.net_qty_for_symbol("TXFD6", strategy_id="MANUAL") == 1


def test_net_qty_for_symbol_legacy_no_strategy_recovery_included_when_no_filter():
    """Legacy recovery without strategy_id still included when no filter."""
    store = _make_position_store()
    store._recovery_positions["acc:TXFD6"] = {
        "symbol": "TXFD6",
        "net_qty": 3,
    }
    # No filter: alpha(+2) + beta(-1) + legacy(+3) = +4
    assert store.net_qty_for_symbol("TXFD6") == 4


def test_net_qty_for_symbol_legacy_no_strategy_recovery_excluded_with_filter():
    """Legacy recovery without strategy_id excluded when filtering specific strategy."""
    store = _make_position_store()
    store._recovery_positions["acc:TXFD6"] = {
        "symbol": "TXFD6",
        "net_qty": 3,
        # No strategy_id key
    }
    # Filter alpha: only alpha's +2 (legacy recovery excluded)
    assert store.net_qty_for_symbol("TXFD6", strategy_id="alpha") == 2
