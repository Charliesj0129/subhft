import time
import pytest
from hft_platform.rust_core import FastGate as RustGate
from hft_platform.risk.fast_gate import FastGate as NumbaGate

ITERATIONS = 1_000_000

def benchmark_rust_vs_numba():
    print("\n[Benchmarking Risk Gate: Rust vs Numba]")
    
    # 1. Rust Setup
    # rust_gate = RustGate("rust_kill_switch", 10000.0, 100.0)
    # Rust constructor might create file, so we use a distinct name
    r_gate = RustGate("rust_ks", 99999.0, 100.0)
    
    # Warmup
    r_gate.check(100.0, 1.0)
    
    start_r = time.perf_counter_ns()
    for _ in range(ITERATIONS):
        r_gate.check(100.0, 1.0)
    end_r = time.perf_counter_ns()
    
    avg_r = (end_r - start_r) / ITERATIONS
    print(f"Rust Latency:  {avg_r:.2f} ns")
    
    # 2. Numba Setup (reuse existing logic)
    n_gate = NumbaGate(max_price=99999.0, max_qty=100.0, create_shm=True)
    n_gate.check(100.0, 1.0) # Warmup
    
    start_n = time.perf_counter_ns()
    for _ in range(ITERATIONS):
        n_gate.check(100.0, 1.0)
    end_n = time.perf_counter_ns()
    
    avg_n = (end_n - start_n) / ITERATIONS
    print(f"Numba Latency: {avg_n:.2f} ns")
    
    # 3. Ratio
    speedup = avg_n / avg_r
    print(f"Speedup: {speedup:.2f}x")
    
    # Functional Check
    r_gate.set_kill_switch(True)
    ok, code = r_gate.check(100.0, 1.0)
    assert not ok and code == 1, "Rust Kill Switch Failed"
    
    # Cleanup
    # n_gate handles own unlink
    
if __name__ == "__main__":
    benchmark_rust_vs_numba()
