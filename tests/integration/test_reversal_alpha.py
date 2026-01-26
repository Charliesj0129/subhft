
import pytest
import numpy as np
import time
from rust_core import LimitOrderBook, AlphaRegimeReversal

def test_reversal_logic():
    print("Testing Rust AlphaRegimeReversal...")
    
    # 1. Initialize
    # VolWindow=100, VolThreshold=0.001, SMAWindow=20
    alpha = AlphaRegimeReversal(100, 0.001, 20)
    lob = LimitOrderBook("ETHUSDT")
    
    mid = 1000.0
    prev_bid = None
    prev_ask = None
    
    # helper
    def update_price(price):
        nonlocal prev_bid, prev_ask, mid
        if prev_bid: lob.update(True, prev_bid, 0.0)
        if prev_ask: lob.update(False, prev_ask, 0.0)
        
        bid = price - 0.5
        ask = price + 0.5
        lob.update(True, bid, 1.0)
        lob.update(False, ask, 1.0)
        prev_bid = bid
        prev_ask = ask
        mid = price

    # 2. Phase 1: Low Volatility (Constant)
    print("Phase 1: Low Volatility")
    for i in range(150):
        update_price(1000.0)
        signal = alpha.calculate(lob)
        vol = alpha.current_vol
        ma = alpha.current_ma
        
    print(f"Low Vol Final: Vol={vol:.6f}, MA={ma:.2f}, Signal={signal:.4f}")
    assert vol < 0.001, "Vol should be low"
    assert signal == 0.0, "Signal should be gated"
    
    # 3. Phase 2: High Volatility (Oscillation)
    # We want to create High Volatility, but trigger Reversion.
    # Reversion Signal = -(Mid - MA) / MA
    # If Price Jumps UP, Signal should be DOWN (Negative).
    print("\nPhase 2: High Volatility")
    
    # Create Volatility first
    for i in range(150):
        p = 1000.0 + (5.0 if i % 2 == 0 else -5.0)
        update_price(p)
        alpha.calculate(lob)
        
    print(f"High Vol Check: Vol={alpha.current_vol:.6f}")
    assert alpha.current_vol > 0.001, "Vol should be high"
    
    # Now create a divergence
    # MA is around 1000.0 (average of +/- 5)
    # Jump Price to 1020.0 (Large Up Move)
    # Expectation: Signal should be Negative (Sell)
    
    update_price(1020.0)
    signal = alpha.calculate(lob)
    ma = alpha.current_ma
    vol = alpha.current_vol
    
    expected_dev = (1020.0 - ma) / ma
    print(f"Jump State: Price=1020.0, MA={ma:.2f}, Signal={signal:.4f}")
    
    assert vol > 0.001
    assert signal < -0.01, f"Signal {signal} should be negative (Reversion)"
    assert np.isclose(signal, -expected_dev, atol=1e-4)
    
    print("\nSUCCESS: Regime-Gated Reversal functioning correctly.")

if __name__ == "__main__":
    test_reversal_logic()
