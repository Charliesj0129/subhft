
import sys
import os
import numpy as np

# Ensure we can import from src
sys.path.append(os.getcwd())

from research.tools.factor_registry import (
    OFIFactor, TransientRepriceFactor, MarkovTransitionFactor, HAS_RUST_CORE
)

def verify_rust_deployment():
    print(f"HAS_RUST_CORE: {HAS_RUST_CORE}")
    
    if not HAS_RUST_CORE:
        print("FAILED: Rust Core not detected!")
        sys.exit(1)
        
    # Create dummy data
    n = 1000
    bid_p = np.linspace(100, 101, n).reshape(-1, 1)
    ask_p = np.linspace(100.01, 101.01, n).reshape(-1, 1)
    bid_v = np.ones((n, 1))
    ask_v = np.ones((n, 1))
    
    data = {
        "bid_prices": bid_p,
        "ask_prices": ask_p,
        "bid_volumes": bid_v,
        "ask_volumes": ask_v,
    }
    
    # Test OFI
    print("Testing OFI (Rust)...")
    ofi = OFIFactor().compute(data)
    assert len(ofi) == n
    print("OFI: OK")
    
    # Test TransientReprice
    print("Testing TransientReprice (Rust)...")
    trans = TransientRepriceFactor().compute(data)
    assert len(trans) == n
    print("TransientReprice: OK")
    
    # Test MarkovTransition
    print("Testing MarkovTransition (Rust)...")
    markov = MarkovTransitionFactor().compute(data)
    assert len(markov) == n
    print("MarkovTransition: OK")
    
    print("\nSUCCESS: All factors using Rust implementation.")

if __name__ == "__main__":
    verify_rust_deployment()
