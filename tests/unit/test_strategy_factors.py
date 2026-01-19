import unittest

from hft_platform.strategy.factors import (
    create_amihud_estimator,
    create_roll_estimator,
    get_emd,
    get_hurst,
    micro_price,
    mid_price,
    price_entropy,
)

# Note: get_hurts might be typo in factors.py? checked file: line 107 `def get_hurst`.
# Import should match definition.


class TestFactors(unittest.TestCase):
    def test_mid_price_list_format(self):
        lob = {"bids": [[100, 10], [99, 10]], "asks": [[102, 10], [103, 10]]}
        # Mid = (100 + 102) / 2 = 101
        self.assertEqual(mid_price(lob), 101.0)

    def test_mid_price_dict_format(self):
        lob = {"bids": [{"price": 100, "volume": 10}], "asks": [{"price": 102, "volume": 10}]}
        self.assertEqual(mid_price(lob), 101.0)

    def test_mid_price_empty(self):
        self.assertTrue(str(mid_price({})).lower() == "nan")

    def test_micro_price(self):
        # Micro price uses simple formula or stoikov.
        # Verify it runs without error and returns float.
        lob = {"bids": [[100, 10]], "asks": [[102, 10]]}
        mp = micro_price(lob)
        self.assertIsInstance(mp, float)
        # Should be between bid and ask
        self.assertTrue(100 <= mp <= 102)

    def test_entropy(self):
        lob = {
            "bids": [[100, 100], [99, 100]],  # Uniform
            "asks": [[102, 100], [103, 100]],
        }
        ent = price_entropy(lob)
        self.assertIsInstance(ent, float)
        self.assertGreater(ent, 0)

    def test_emd(self):
        lob1 = {"bids": [[100, 10]], "asks": []}
        lob2 = {"bids": [[100, 20]], "asks": []}
        dist = get_emd(lob1, lob2)
        self.assertIsInstance(dist, float)

    def test_hurst(self):
        series = [1, 2, 3, 4, 5] * 20
        h = get_hurst(series)
        self.assertIsInstance(h, float)

    def test_factories(self):
        self.assertIsNotNone(create_roll_estimator())
        self.assertIsNotNone(create_amihud_estimator())
