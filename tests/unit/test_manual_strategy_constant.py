"""Tests for MANUAL_STRATEGY_ID constant and its usage contract."""


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
