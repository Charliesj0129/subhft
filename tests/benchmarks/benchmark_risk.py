import time
import numpy as np
from src.hft_platform.risk.fast_gate import FastGate

ITERATIONS = 1_000_000

def benchmark_risk_gate():
    print("[Benchmarking FastGate]")
    
    # Initialize Gate with creating SHM (Owner)
    gate = FastGate(max_price=99999.0, max_qty=100.0, create_shm=True)
    
    # Warmup
    gate.check(100.0, 1.0)
    
    start = time.perf_counter_ns()
    
    for i in range(ITERATIONS):
        # Passed check
        gate.check(100.0, 1.0)
        
    end = time.perf_counter_ns()
    total_ns = end - start
    
    print(f"Total: {total_ns} ns")
    print(f"Avg Latency: {(total_ns)/ITERATIONS:.2f} ns")
    
    # Verify rejection (sanity check, excluded from bench loop)
    # 1. Price Max
    ok, code = gate.check(100000.0, 1.0)
    assert ok is False and code == 3, f"Failed Price Max Check: {code}"
    
    # 2. Kill Switch
    gate.set_kill_switch(True)
    ok, code = gate.check(100.0, 1.0)
    assert ok is False and code == 1, f"Failed Kill Switch Check: {code}"
    
    print("Functional Checks Passed.")
    gate.close()
    gate.unlink()

if __name__ == "__main__":
    benchmark_risk_gate()
