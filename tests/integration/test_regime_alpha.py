import pytest

rust_core = pytest.importorskip("rust_core", reason="rust_core extension not built")
if not hasattr(rust_core, "AlphaRegimePressure"):
    pytest.skip("AlphaRegimePressure not available in rust_core build", allow_module_level=True)

AlphaRegimePressure = rust_core.AlphaRegimePressure
LimitOrderBook = rust_core.LimitOrderBook


def test_regime_logic():
    print("Testing Rust AlphaRegimePressure...")

    # 1. Initialize
    # Window=100 for Volatility, Threshold=0.001
    alpha = AlphaRegimePressure(100, 0.001)
    lob = LimitOrderBook("ETHUSDT")

    # Setup initial LOB
    lob.update(True, 1000.0, 1.0)
    lob.update(False, 1001.0, 1.0)

    # 2. Phase 1: Low Volatility (Constant Mid)
    print("Phase 1: Low Volatility (Constant Price)")
    for i in range(200):
        # Update LOB but keep mid price constant (1000.5)
        # Just update sizes to create pressure signal, but vol should be zero
        lob.update(True, 1000.0, 10.0 + i % 2)  # Varied size
        lob.update(False, 1001.0, 5.0)  # Constant size

        # Calculate
        signal = alpha.calculate(lob)
        vol = alpha.current_vol

        if i % 50 == 0:
            print(f"Tick {i}: Vol={vol:.6f}, Signal={signal:.4f}")

    # Expectation: Vol should decay to 0. Signal should be 0.0 (Gated).
    assert vol < 0.001, f"Vol {vol} should be Low"
    assert signal == 0.0, "Signal should be gated (0.0)"

    # 3. Phase 2: High Volatility (Random Walk)
    print("\nPhase 2: High Volatility (Price Jumps)")
    mid = 1000.5
    prev_bid = None
    prev_ask = None

    for i in range(200):
        # 1. Clear previous levels
        if prev_bid:
            lob.update(True, prev_bid, 0.0)
        if prev_ask:
            lob.update(False, prev_ask, 0.0)

        # 2. Move price significantly
        step = 5.0 * (1 if i % 2 == 0 else -1)
        mid += step

        bid = mid - 0.5
        ask = mid + 0.5

        lob.update(True, bid, 100.0)  # Bid Pressure (High Bid Vol)
        lob.update(False, ask, 1.0)  # Low Ask Vol

        prev_bid = bid
        prev_ask = ask

        signal = alpha.calculate(lob)
        vol = alpha.current_vol

        if i > 150 and i % 10 == 0:
            print(f"Tick {i}: Mid={mid:.1f}, Vol={vol:.6f}, Signal={signal:.4f}")

    # Expectation: Vol should spike. Signal should be active (BidVol - AskVol = 99.0)
    # Vol of +/- 0.5% oscillation should be ~0.005
    assert vol > 0.001, f"Vol {vol} should be High (>0.001)"
    assert signal == 99.0, f"Signal {signal} should be active (99.0)"

    print("\nSUCCESS: Regime Gate functioning correctly.")


if __name__ == "__main__":
    test_regime_logic()
