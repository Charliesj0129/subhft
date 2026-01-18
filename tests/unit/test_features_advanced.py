import math

from hft_platform.features.advanced_liquidity import RollingAmihud, RollingEffectiveSpread
from hft_platform.features.entropy import earth_mover_distance, lob_entropy
from hft_platform.features.fractal import hurst_exponent
from hft_platform.features.ofi import OFICalculator


def test_entropy():
    # Uniform Dist: 3 items [10, 10, 10]
    # Probs = [1/3, 1/3, 1/3]
    # Entropy = -3 * (1/3 log2(1/3)) = -log2(1/3) = log2(3) ≈ 1.585
    lob = {"bids": [[100, 10], [99, 10], [98, 10]], "asks": []}
    e = lob_entropy(lob, depth=3)
    assert abs(e - math.log2(3)) < 0.001

    # Concentrated: [30, 0, 0] -> Entropy 0
    lob2 = {"bids": [[100, 30], [99, 0], [98, 0]], "asks": []}
    e2 = lob_entropy(lob2, depth=3)
    assert e2 == 0.0


def test_emd():
    # Dist1: [0.5, 0.5] (CDF: 0.5, 1.0)
    # Dist2: [1.0, 0.0] (CDF: 1.0, 1.0)
    # Diff CDF: |0.5-1.0| + |1.0-1.0| = 0.5 + 0 = 0.5
    d1 = [0.5, 0.5]
    d2 = [1.0, 0.0]
    emd = earth_mover_distance(d1, d2)
    assert abs(emd - 0.5) < 0.001


def test_hurst():
    # Random Walk (Brownian) -> H ~ 0.5
    # Linear Trend -> H ~ 1.0
    # Mean Reversion -> H < 0.5

    # Perfect trend
    trend = [float(x) for x in range(100)]
    h = hurst_exponent(trend)
    # R increases linearly with M. S is relatively constant-ish (scaling).
    # For linear trend, H should be close to 1.
    assert h > 0.85

    # Note: R/S is noisy on small samples.


def test_roll_spread():
    roll = RollingEffectiveSpread(window_size=100)

    # Bounce: 100, 101, 100, 101...
    # Delta: +1, -1, +1, -1...
    # Cov(dt, dt-1) should be negative.
    # Cov([1, -1], [-1, 1]) approx -1.
    # Spread = 2 * sqrt(-(-1)) = 2.

    prices = [100, 101] * 50
    for p in prices:
        s = roll.update(float(p))

    # After enough samples
    assert abs(s - 2.0) < 0.1


def test_amihud():
    ami = RollingAmihud(window_size=10)
    # Price 100, Vol 10, Ret 0.01 (1%)
    # Ratio = 0.01 / 1000 = 0.00001
    for _ in range(10):
        val = ami.update(0.01, 100.0, 10.0)

    assert abs(val - 0.00001) < 1e-9


def test_ofi_decomposition():
    ofi = OFICalculator(depth=1)

    # T0: Bid 100x10
    lob0 = {"bids": [[100, 10]], "asks": [[102, 10]]}
    ofi.update(lob0)

    # T1: Bid 100x5 (Drop 5). Trade Vol = 2.
    # Logic: 5 dropped. 2 explained by trade. 3 by cancel.
    lob1 = {"bids": [[100, 5]], "asks": [[102, 10]]}

    # Bid Cancel = -3. Bid Trade = -2. Total = -5.

    val = ofi.update(lob1, trade_vol=2.0)
    assert val == -5.0  # Total OFI correct

    # Check decomposition
    d = ofi.last_decompose
    # Expected:
    # ofi_total = -5
    # ofi_limit = 0 (+Limit -Limit)
    # ofi_trade = -2.0 (Bid removes due to trade)
    # ofi_cancel = -3.0 (Remainder)

    assert d["ofi_total"] == -5.0
    assert abs(d["ofi_trade"] - (-2.0)) < 1e-9
    assert abs(d["ofi_cancel"] - (-3.0)) < 1e-9
    assert abs(d["ofi_limit"] - 0.0) < 1e-9
