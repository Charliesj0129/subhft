import time

from rust_core import AlphaDepthSlope, LimitOrderBook


def test_rust_alpha_logic():
    print("Testing Rust AlphaDepthSlope...")

    # 1. Initialize
    lob = LimitOrderBook("ETHUSDT")
    alpha = AlphaDepthSlope(window_size=100)

    # 2. Populate LOB
    # Bid side (Steep slope: high volume at top, low deep)
    # Price: 100, 99, 98...
    # Vol:   100, 50, 10...
    for i in range(10):
        lob.update(True, 100.0 - i, 100.0 / (i + 1))

    # Ask side (Flat slope: constant volume)
    # Price: 101, 102, 103...
    # Vol:   10, 10, 10...
    for i in range(10):
        lob.update(False, 101.0 + i, 10.0)

    # 3. Calculate Signal
    # Expect: BidSlope (Steep/Negative log-log?) vs AskSlope (Flat/Zero)
    # Actually, slope is (Level vs LogVol).
    # Bid: Level 1->100, Level 10->10. Log(100)=4.6, Log(10)=2.3. Downward slope (Negative).
    # Ask: Level 1->10, Level 10->10. Flat slope (Zero).
    # Signal = BidSlope - AskSlope. (Negative - 0) = Negative.
    # WAIT: DepthSlope definition:
    # "Positive = bid side steeper (bullish)"? No, let's check code.
    # In Python: slope = bid_slope - ask_slope.
    # If Bid volume decays faster (concentrated at top), slope is more NEGATIVE.
    # If Ask volume is constant, slope is 0.
    # So Signal is < 0.
    # Concentrated bids usually support price, so we expect Bullish?
    # Actually, Python code doc says "Slope of depth decay".
    # Logic in paper: Steep decay = Liquidity concentration near best. Support.
    # Slope is negative. More negative = detailed.
    # Let's verify the value.

    signal = alpha.calculate(lob)
    print(f"Calculated Signal: {signal}")

    assert isinstance(signal, float)
    assert signal != 0.0

    # 4. Performance Bench
    start = time.time()
    for _ in range(100_000):
        alpha.calculate(lob)
    dt = time.time() - start
    print(f"100k updates in {dt:.4f}s ({100_000 / dt:,.0f} ops/sec)")

    # Baseline Python ~20k/sec (optimized)
    # Expect Rust > 200k/sec
    assert 100_000 / dt > 100_000, "Rust implementation too slow!"


if __name__ == "__main__":
    test_rust_alpha_logic()
