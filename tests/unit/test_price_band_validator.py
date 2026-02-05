"""Unit tests for PriceBandValidator with LOB-relative price validation."""

import unittest
from unittest.mock import MagicMock, patch

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.core.pricing import FixedPriceScaleProvider
from hft_platform.risk.validators import PriceBandValidator

# Default scale factor is 10,000 (from FixedPriceScaleProvider)
# So $100.00 -> 1,000,000 scaled
SCALE = 10_000


class TestPriceBandValidatorBasic(unittest.TestCase):
    """Test basic PriceBandValidator functionality."""

    def setUp(self):
        self.config = {
            "global_defaults": {
                "max_price_cap": 5000.0,  # $5000 max -> 50,000,000 scaled
                "price_band_ticks": 20,
                "tick_size": 0.01,  # 1 cent tick -> 100 scaled
            },
            "strategies": {},
        }
        # Mock the metrics registry
        self.metrics_patcher = patch("hft_platform.risk.validators.MetricsRegistry.get")
        self.mock_metrics = self.metrics_patcher.start()
        self.mock_metrics.return_value = MagicMock()

    def tearDown(self):
        self.metrics_patcher.stop()

    _intent_counter = 0

    def _make_intent(
        self,
        price: int,
        qty: int = 100,
        symbol: str = "2330",
        intent_type: IntentType = IntentType.NEW,
    ) -> OrderIntent:
        """Helper to create OrderIntent for testing."""
        TestPriceBandValidatorBasic._intent_counter += 1
        return OrderIntent(
            intent_id=TestPriceBandValidatorBasic._intent_counter,
            strategy_id="test_strategy",
            symbol=symbol,
            side=Side.BUY,
            price=price,
            qty=qty,
            intent_type=intent_type,
        )

    def test_cancel_always_approved(self):
        """CANCEL intents should always be approved."""
        validator = PriceBandValidator(self.config)
        intent = self._make_intent(price=0, intent_type=IntentType.CANCEL)

        approved, reason = validator.check(intent)

        self.assertTrue(approved)
        self.assertEqual(reason, "OK")

    def test_zero_price_rejected(self):
        """Zero price should be rejected."""
        validator = PriceBandValidator(self.config)
        intent = self._make_intent(price=0)

        approved, reason = validator.check(intent)

        self.assertFalse(approved)
        self.assertEqual(reason, "PRICE_ZERO_OR_NEG")

    def test_negative_price_rejected(self):
        """Negative price should be rejected."""
        validator = PriceBandValidator(self.config)
        intent = self._make_intent(price=-100)

        approved, reason = validator.check(intent)

        self.assertFalse(approved)
        self.assertEqual(reason, "PRICE_ZERO_OR_NEG")

    def test_price_exceeds_cap_rejected(self):
        """Price exceeding max cap should be rejected."""
        validator = PriceBandValidator(self.config)
        # max_price_cap=5000.0 with scale=10,000 -> 50,000,000 scaled
        # Create price that exceeds this
        intent = self._make_intent(price=60_000_000)  # $6000 scaled

        approved, reason = validator.check(intent)

        self.assertFalse(approved)
        self.assertIn("PRICE_EXCEEDS_CAP", reason)

    def test_valid_price_approved_without_lob(self):
        """Valid price should be approved when no LOB available."""
        validator = PriceBandValidator(self.config)
        # Price of $100 scaled (1,000,000) is under cap
        intent = self._make_intent(price=1_000_000)

        approved, reason = validator.check(intent)

        self.assertTrue(approved)
        self.assertEqual(reason, "OK")


class TestPriceBandValidatorWithLOB(unittest.TestCase):
    """Test PriceBandValidator with LOB-relative validation.

    Key scaling facts (default scale = 10,000):
    - $100.00 -> 1,000,000 scaled
    - tick_size = 0.01 -> 100 scaled
    - 20 ticks * 100 = 2,000 scaled band
    """

    def setUp(self):
        self.config = {
            "global_defaults": {
                "max_price_cap": 5000.0,  # $5000 -> 50,000,000 scaled
                "price_band_ticks": 20,
                "tick_size": 0.01,  # $0.01 -> 100 scaled
            },
            "strategies": {
                "aggressive_strategy": {
                    "price_band_ticks": 50,  # More permissive
                },
            },
        }
        # Mock metrics
        self.metrics_patcher = patch("hft_platform.risk.validators.MetricsRegistry.get")
        self.mock_metrics = self.metrics_patcher.start()
        self.mock_metrics.return_value = MagicMock()

        # Mock LOB engine
        self.mock_lob = MagicMock()

    def tearDown(self):
        self.metrics_patcher.stop()

    _intent_counter = 0

    def _make_intent(
        self,
        price: int,
        qty: int = 100,
        symbol: str = "2330",
        strategy_id: str = "test_strategy",
        intent_type: IntentType = IntentType.NEW,
    ) -> OrderIntent:
        TestPriceBandValidatorWithLOB._intent_counter += 1
        return OrderIntent(
            intent_id=TestPriceBandValidatorWithLOB._intent_counter,
            strategy_id=strategy_id,
            symbol=symbol,
            side=Side.BUY,
            price=price,
            qty=qty,
            intent_type=intent_type,
        )

    def test_price_within_band_approved(self):
        """Price within band around mid price should be approved."""
        # Mid price = 1,000,000 (= $100.00 with scale=10,000, already scaled)
        # Band = 20 ticks * 0.01 * 10,000 scale = 2,000
        # Valid range: [998,000, 1,002,000]
        self.mock_lob.get_book_snapshot.return_value = {
            "symbol": "2330",
            "mid_price": 1_000_000.0,  # Already scaled (mid_price_x2 / 2.0)
        }

        validator = PriceBandValidator(self.config, lob=self.mock_lob)
        intent = self._make_intent(price=1_000_000)  # Exactly mid price

        approved, reason = validator.check(intent)

        self.assertTrue(approved)
        self.assertEqual(reason, "OK")

    def test_price_at_band_edge_approved(self):
        """Price at edge of band should be approved."""
        self.mock_lob.get_book_snapshot.return_value = {
            "symbol": "2330",
            "mid_price": 1_000_000.0,  # Already scaled
        }

        validator = PriceBandValidator(self.config, lob=self.mock_lob)
        # Band = 20 * 0.01 * 10,000 = 2,000
        # Price at lower edge: 1,000,000 - 2,000 = 998,000
        intent = self._make_intent(price=998_000)

        approved, reason = validator.check(intent)

        self.assertTrue(approved)

    def test_price_outside_band_rejected(self):
        """Price outside band should be rejected."""
        self.mock_lob.get_book_snapshot.return_value = {
            "symbol": "2330",
            "mid_price": 1_000_000.0,  # Already scaled
        }

        validator = PriceBandValidator(self.config, lob=self.mock_lob)
        # Band = 20 * 0.01 * 10,000 = 2,000
        # Price way outside: 1,000,000 - 5,000 = 995,000
        intent = self._make_intent(price=995_000)

        approved, reason = validator.check(intent)

        self.assertFalse(approved)
        self.assertIn("PRICE_OUTSIDE_BAND", reason)

    def test_price_above_band_rejected(self):
        """Price above band should be rejected."""
        self.mock_lob.get_book_snapshot.return_value = {
            "symbol": "2330",
            "mid_price": 1_000_000.0,  # Already scaled
        }

        validator = PriceBandValidator(self.config, lob=self.mock_lob)
        # Band = 2,000
        # Price way above: 1,000,000 + 5,000 = 1,005,000
        intent = self._make_intent(price=1_005_000)

        approved, reason = validator.check(intent)

        self.assertFalse(approved)
        self.assertIn("PRICE_OUTSIDE_BAND", reason)

    def test_strategy_specific_band_ticks(self):
        """Strategy-specific band_ticks should be used."""
        self.mock_lob.get_book_snapshot.return_value = {
            "symbol": "2330",
            "mid_price": 1_000_000.0,  # Already scaled
        }

        validator = PriceBandValidator(self.config, lob=self.mock_lob)
        # Use aggressive_strategy with band_ticks=50
        # Band = 50 * 0.01 * 10,000 = 5,000
        # Price outside default band (2,000) but within aggressive band (5,000)
        intent = self._make_intent(price=996_000, strategy_id="aggressive_strategy")

        approved, reason = validator.check(intent)

        self.assertTrue(approved)

    def test_no_mid_price_skips_band_check(self):
        """When no mid price available, band check is skipped."""
        self.mock_lob.get_book_snapshot.return_value = {
            "symbol": "2330",
            "mid_price": 0,  # No mid price
        }

        validator = PriceBandValidator(self.config, lob=self.mock_lob)
        # Price under max_price_cap (5000 * 10,000 = 50,000,000)
        intent = self._make_intent(price=1_000_000)

        approved, reason = validator.check(intent)

        # Should pass because band check is skipped (no reference price)
        self.assertTrue(approved)

    def test_lob_exception_skips_band_check(self):
        """LOB exception should skip band check gracefully."""
        self.mock_lob.get_book_snapshot.side_effect = RuntimeError("LOB error")

        validator = PriceBandValidator(self.config, lob=self.mock_lob)
        # Price under max cap
        intent = self._make_intent(price=1_000_000)

        approved, reason = validator.check(intent)

        # Should pass because band check is skipped on error
        self.assertTrue(approved)


class TestPriceBandValidatorMidPriceRetrieval(unittest.TestCase):
    """Test _get_mid_price method."""

    def setUp(self):
        self.config = {"global_defaults": {}, "strategies": {}}
        self.metrics_patcher = patch("hft_platform.risk.validators.MetricsRegistry.get")
        self.mock_metrics = self.metrics_patcher.start()
        self.mock_metrics.return_value = MagicMock()

    def tearDown(self):
        self.metrics_patcher.stop()

    def test_get_mid_price_returns_scaled_value(self):
        """_get_mid_price should return scaled integer."""
        mock_lob = MagicMock()
        # LOB stores mid_price already in scaled units (mid_price_x2 / 2.0)
        mock_lob.get_book_snapshot.return_value = {
            "symbol": "2330",
            "mid_price": 1_005_000.0,  # Already scaled float ($100.50)
        }

        validator = PriceBandValidator(self.config, lob=mock_lob)
        mid_price = validator._get_mid_price("2330")

        # mid_price is already scaled, just converted to int
        self.assertEqual(mid_price, 1_005_000)

    def test_get_mid_price_returns_none_on_error(self):
        """_get_mid_price should return None on error."""
        mock_lob = MagicMock()
        mock_lob.get_book_snapshot.side_effect = Exception("Error")

        validator = PriceBandValidator(self.config, lob=mock_lob)
        mid_price = validator._get_mid_price("2330")

        self.assertIsNone(mid_price)

    def test_get_mid_price_returns_none_when_no_lob(self):
        """_get_mid_price should return None when no LOB."""
        validator = PriceBandValidator(self.config, lob=None)
        mid_price = validator._get_mid_price("2330")

        self.assertIsNone(mid_price)


if __name__ == "__main__":
    unittest.main()
