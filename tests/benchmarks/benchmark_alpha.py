import time
import numpy as np
from strategies.alpha.alpha_ofi import AlphaOFI
from strategies.alpha.alpha_hawkes import HawkesTracker

def benchmark_ofi():
    print("[Benchmarking AlphaOFI]")
    # JIT compilation warmup
    alpha = AlphaOFI()
    alpha.update(100.0, 101.0, 1.0, 1.0)
    
    # Bench parameters
    iterations = 1_000_000
    
    start = time.perf_counter_ns()
    
    # Simulation loop
    # We want to measure the pure update cost, not just the python loop overhead.
    # But since we are calling from Python, there is dispatch overhead.
    # To truly measure 'inside numba' cost, we should wrap this loop in Numba too,
    # but that measures the 'strategy' perspective.
    # The requirement is "< 2us per tick".
    
    for i in range(iterations):
        # Toggle prices to force calculation
        px = 100.0 + (i % 2)
        alpha.update(px, px + 1.0, 10.0, 10.0)
        
    end = time.perf_counter_ns()
    
    total_ns = end - start
    avg_ns = total_ns / iterations
    print(f"Total: {total_ns} ns")
    print(f"Iterations: {iterations}")
    print(f"Avg Latency: {avg_ns:.2f} ns")
    
    if avg_ns > 2000:
        print("❌ FAILED Darwin Gate (< 2000 ns)")
    else:
        print("✅ PASSED Darwin Gate (< 2000 ns)")

def benchmark_hawkes():
    print("\n[Benchmarking AlphaHawkes]")
    # JIT Warmup
    tracker = HawkesTracker(1.0, 0.5, 10.0)
    tracker.update(1000, True)
    
    iterations = 1_000_000
    current_ts = 1000
    
    start = time.perf_counter_ns()
    
    for i in range(iterations):
        current_ts += 1000 # 1us
        tracker.update(current_ts, i % 10 == 0)
        
    end = time.perf_counter_ns()
    
    total_ns = end - start
    avg_ns = total_ns / iterations
    print(f"Total: {total_ns} ns")
    print(f"Iterations: {iterations}")
    print(f"Avg Latency: {avg_ns:.2f} ns")
    
    if avg_ns > 2000:
        print("❌ FAILED Darwin Gate (< 2000 ns)")
    else:
        print("✅ PASSED Darwin Gate (< 2000 ns)")

def benchmark_mm_hawkes():
    print("\n[Benchmarking MM_HAWKES (Hawkes + Propagator)]")
    from strategies.mm_hawkes import HawkesTracker, PropagatorTracker
    
    # JIT Warmup
    hawkes = HawkesTracker(1.0, 0.5, 10.0)
    prop = PropagatorTracker()
    
    hawkes.update(1000, True)
    prop.update(1000)
    prop.add_event(1.0, 10.0)
    
    iterations = 1_000_000
    current_ts = 1000
    
    start = time.perf_counter_ns()
    
    for i in range(iterations):
        current_ts += 1000 # 1us
        
        # Simulate strategy update loop
        hawkes.update(current_ts, i % 10 == 0)
        prop.update(current_ts)
        
        # Simulate trade event
        if i % 10 == 0:
            prop.add_event(1.0, 5.0)
            
        # Simulate Quote Calculation (Spread + Skew)
        spread = 2.0 * (1.0 + 0.5 * hawkes.intensity)
        skew = 0.5 * prop.total_impact
        # Price calc
        bid = 100.0 - skew - spread/2
        ask = 100.0 - skew + spread/2
        
    end = time.perf_counter_ns()
    
    total_ns = end - start
    avg_ns = total_ns / iterations
    print(f"Total: {total_ns} ns")
    print(f"Iterations: {iterations}")
    print(f"Avg Latency: {avg_ns:.2f} ns")
    
    # Requirement: Strategy Logic < 5us (Darwin Gate relaxed for MM)
    if avg_ns > 5000:
        print("❌ FAILED Darwin Gate (< 5000 ns)")
    else:
        print("✅ PASSED Darwin Gate (< 5000 ns)")

if __name__ == "__main__":
    benchmark_hawkes()
    benchmark_mm_hawkes()

