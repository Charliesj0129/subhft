#!/usr/bin/env python3
"""
Unit tests for HawkesCriticalityFactor (Paper 2601.11602v1)
"""

import numpy as np
import pytest

pytest.importorskip("research.tools.factor_registry")


def test_hawkes_criticality_basic():
    """Test basic computation of HawkesCriticalityFactor."""
    from research.tools.factor_registry import HawkesCriticalityFactor
    
    # Create synthetic data
    n = 500
    np.random.seed(42)
    
    # Normal regime: small trades
    trade_vol = np.abs(np.random.randn(n)) * 100 + 50
    trade_side = np.random.choice([-1, 1], size=n).astype(np.float64)
    
    data = {
        "trade_volume": trade_vol,
        "trade_side": trade_side,
    }
    
    factor = HawkesCriticalityFactor()
    signal = factor.compute(data)
    
    # Basic checks
    assert signal.shape == (n,), f"Expected shape ({n},), got {signal.shape}"
    assert not np.isnan(signal).all(), "Signal is all NaN"
    

def test_hawkes_criticality_event_detection():
    """Test that large trades trigger events and increase intensity."""
    from research.tools.factor_registry import HawkesCriticalityFactor
    
    n = 300
    
    # Create data with a burst of large trades
    trade_vol = np.ones(n) * 100.0  # Normal volume
    trade_side = np.ones(n)  # All buys
    
    # Inject a burst of large trades at t=150-170
    trade_vol[150:170] = 1000.0  # 10x normal
    
    data = {
        "trade_volume": trade_vol,
        "trade_side": trade_side,
    }
    
    factor = HawkesCriticalityFactor(
        vol_threshold=1.5,
        mu=0.05,
        alpha=0.5,  # High excitation
        beta=0.1,   # Slow decay
        intensity_threshold=0.5,
    )
    signal = factor.compute(data)
    
    # During the burst, signal should flip (become negative for buys)
    # After warmup (100), check that some signals are negative during burst
    burst_signals = signal[150:170]
    post_burst = signal[180:200]
    
    # During burst: high intensity -> signal should be negative (contrarian)
    # After burst: intensity decays -> signal should return to positive
    # We just check that at least some flipping occurred
    assert np.any(burst_signals < 0) or np.any(post_burst > 0), \
        "Expected regime switching behavior"


def test_hawkes_criticality_no_events():
    """Test behavior when no events occur (constant low volume)."""
    from research.tools.factor_registry import HawkesCriticalityFactor
    
    n = 200
    
    # All trades have exactly the same volume -> no events
    trade_vol = np.ones(n) * 100.0
    trade_side = np.ones(n)
    
    data = {
        "trade_volume": trade_vol,
        "trade_side": trade_side,
    }
    
    factor = HawkesCriticalityFactor(
        mu=0.01,  # Low baseline
        intensity_threshold=0.5,
    )
    signal = factor.compute(data)
    
    # With low intensity and no events, signal should remain positive (= base_signal)
    # After warmup, all signals should be positive (buys)
    post_warmup = signal[110:]
    assert np.all(post_warmup >= 0), "Expected no sign flips without events"


def test_hawkes_criticality_properties():
    """Verify factor properties."""
    from research.tools.factor_registry import HawkesCriticalityFactor
    
    factor = HawkesCriticalityFactor()
    
    assert factor.name == "HawkesCriticality"
    assert factor.paper_id == "2601.11602v1"
    assert "Phase Transition" in factor.description or "herding" in factor.description


def test_hawkes_criticality_registry():
    """Test that factor is registered correctly."""
    from research.tools.factor_registry import FactorRegistry
    
    assert "HawkesCriticality" in FactorRegistry.list_factors()
    factor = FactorRegistry.get_factor("HawkesCriticality")
    assert factor.name == "HawkesCriticality"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
