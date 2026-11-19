import time

from hft_platform.rust_core import AlphaDepthSlope, LimitOrderBook


def test_rust_alpha_logic():
    print("Testing Rust AlphaDepthSlope...")

    lob = LimitOrderBook("ETHUSDT")
    alpha = AlphaDepthSlope(window_size=100)

    for i in range(10):
        lob.update(True, 100.0 - i, 100.0 / (i + 1))

    for i in range(10):
        lob.update(False, 101.0 + i, 10.0)

    signal = alpha.calculate(lob)
    print(f"Calculated Signal: {signal}")

    assert isinstance(signal, float)
    assert signal != 0.0

    start = time.time()
    for _ in range(100_000):
        alpha.calculate(lob)
    dt = time.time() - start
    print(f"100k updates in {dt:.4f}s ({100_000 / dt:,.0f} ops/sec)")

    assert 100_000 / dt > 100_000, "Rust implementation too slow!"


if __name__ == "__main__":
    test_rust_alpha_logic()
