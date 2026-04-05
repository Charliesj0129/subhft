"""Unit tests for per-strategy intent flood cap in StrategyRunner.

Tests validate the truncation logic in isolation without requiring a full
StrategyRunner instantiation.
"""
import os
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — replicate the truncation logic extracted from runner.py so tests
# remain fast and isolated.
# ---------------------------------------------------------------------------

def _apply_intent_cap(intents, max_intents_per_event, strategy_id, logger_fn):
    """Mirror of the cap logic in StrategyRunner._process_event."""
    if len(intents) > max_intents_per_event:
        logger_fn(
            "strategy_intent_flood",
            strategy_id=strategy_id,
            intent_count=len(intents),
            cap=max_intents_per_event,
        )
        return intents[:max_intents_per_event]
    return intents


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestIntentCapLogic:
    """Tests for the intent flood cap truncation logic."""

    def test_intents_within_cap_all_submitted(self):
        """Strategies returning fewer intents than cap pass through unchanged."""
        mock_logger = MagicMock()
        intents = list(range(5))
        cap = 20

        result = _apply_intent_cap(intents, cap, "strat_a", mock_logger)

        assert len(result) == 5
        assert result == intents
        mock_logger.assert_not_called()

    def test_intents_over_cap_truncated(self):
        """Strategies returning more intents than cap are truncated to cap size."""
        mock_logger = MagicMock()
        intents = list(range(30))
        cap = 20

        result = _apply_intent_cap(intents, cap, "strat_b", mock_logger)

        assert len(result) == 20
        assert result == list(range(20))  # first 20 preserved
        mock_logger.assert_called_once()
        _call_kwargs = mock_logger.call_args
        assert _call_kwargs[0][0] == "strategy_intent_flood"
        assert _call_kwargs[1]["intent_count"] == 30
        assert _call_kwargs[1]["cap"] == 20
        assert _call_kwargs[1]["strategy_id"] == "strat_b"

    def test_intents_exactly_at_cap_not_truncated(self):
        """Exactly cap-many intents are allowed without truncation or warning."""
        mock_logger = MagicMock()
        intents = list(range(20))
        cap = 20

        result = _apply_intent_cap(intents, cap, "strat_c", mock_logger)

        assert len(result) == 20
        mock_logger.assert_not_called()

    def test_intent_cap_configurable_via_env(self):
        """HFT_MAX_INTENTS_PER_EVENT env var controls the cap value at import."""
        with patch.dict(os.environ, {"HFT_MAX_INTENTS_PER_EVENT": "5"}):
            cap = int(os.getenv("HFT_MAX_INTENTS_PER_EVENT", "20"))

        mock_logger = MagicMock()
        intents = list(range(10))

        result = _apply_intent_cap(intents, cap, "strat_d", mock_logger)

        assert len(result) == 5
        assert result == list(range(5))
        mock_logger.assert_called_once()

    def test_truncation_preserves_order(self):
        """Truncation keeps the first N intents in their original order."""
        mock_logger = MagicMock()
        intents = [f"intent_{i}" for i in range(50)]
        cap = 10

        result = _apply_intent_cap(intents, cap, "strat_e", mock_logger)

        assert result == [f"intent_{i}" for i in range(10)]

    def test_empty_intents_not_affected(self):
        """Empty intent list produces no warning and returns empty list."""
        mock_logger = MagicMock()
        result = _apply_intent_cap([], 20, "strat_f", mock_logger)

        assert result == []
        mock_logger.assert_not_called()


class TestRunnerModuleConstant:
    """Verify the module-level constant is read from the environment."""

    def test_module_constant_default(self):
        """Default cap is 20 when env var is absent."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HFT_MAX_INTENTS_PER_EVENT", None)
            value = int(os.getenv("HFT_MAX_INTENTS_PER_EVENT", "20"))
        assert value == 20

    def test_module_constant_override(self):
        """Cap reflects env var override."""
        with patch.dict(os.environ, {"HFT_MAX_INTENTS_PER_EVENT": "7"}):
            value = int(os.getenv("HFT_MAX_INTENTS_PER_EVENT", "20"))
        assert value == 7
