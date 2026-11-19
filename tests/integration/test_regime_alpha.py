import pytest

rust_core = pytest.importorskip("rust_core", reason="rust_core extension not built")
if not hasattr(rust_core, "AlphaRegimePressure"):
    pytest.skip("AlphaRegimePressure not available in rust_core build", allow_module_level=True)

AlphaRegimePressure = rust_core.AlphaRegimePressure
LimitOrderBook = rust_core.LimitOrderBook


def test_regime_logic():
    print("Testing Rust AlphaRegimePressure...")

    alpha = AlphaRegimePressure(100, 0.001)
    lob = LimitOrderBook("ETHUSDT")

    lob.update(True, 1000.0, 1.0)
    lob.update(False, 1001.0, 1.0)

    print("Phase 1: Low Volatility (Constant Price)")
    for i in range(200):
        lob.update(True, 1000.0, 10.0 + i % 2)
        lob.update(False, 1001.0, 5.0)

        signal = alpha.calculate(lob)
        vol = alpha.current_vol

        if i % 50 == 0:
            print(f"Tick {i}: Vol={vol:.6f}, Signal={signal:.4f}")

    assert vol < 0.001, f"Vol {vol} should be Low"
    assert signal == 0.0, "Signal should be gated (0.0)"

    print("\nPhase 2: High Volatility (Price Jumps)")
    mid = 1000.5
    prev_bid = None
    prev_ask = None

    for i in range(200):
        if prev_bid:
            lob.update(True, prev_bid, 0.0)
        if prev_ask:
            lob.update(False, prev_ask, 0.0)

        step = 5.0 * (1 if i % 2 == 0 else -1)
        mid += step

        bid = mid - 0.5
        ask = mid + 0.5

        lob.update(True, bid, 100.0)
        lob.update(False, ask, 1.0)

        prev_bid = bid
        prev_ask = ask

        signal = alpha.calculate(lob)
        vol = alpha.current_vol

        if i > 150 and i % 10 == 0:
            print(f"Tick {i}: Mid={mid:.1f}, Vol={vol:.6f}, Signal={signal:.4f}")

    assert vol > 0.001, f"Vol {vol} should be High (>0.001)"
    assert signal == 99.0, f"Signal {signal} should be active (99.0)"

    print("\nSUCCESS: Regime Gate functioning correctly.")


if __name__ == "__main__":
    test_regime_logic()
