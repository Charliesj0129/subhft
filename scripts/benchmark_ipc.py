import time
import multiprocessing
import numpy as np
import os
from src.hft_platform.ipc.shm_ring_buffer import ShmRingBuffer

ITERATIONS = 1_000_000
SHM_NAME = "hft_benchmark_ring"

def producer():
    # Producer: Writes integers 0..ITERATIONS
    rb = ShmRingBuffer(SHM_NAME, create=False)
    
    # Warmup
    payload = b'\x01' * 64
    
    start = time.perf_counter_ns()
    
    for i in range(ITERATIONS):
        # Busy spin if full
        while not rb.write(payload):
            pass
            
    end = time.perf_counter_ns()
    print(f"[Producer] Done. Avg Write Time (overhead only): {(end-start)/ITERATIONS:.2f} ns")
    rb.close()

def consumer():
    # Consumer: Reads until done
    rb = ShmRingBuffer(SHM_NAME, create=True)
    count = 0
    
    # Wait for producer to be ready (simplistic sync)
    time.sleep(1)
    
    start = time.perf_counter_ns()
    
    while count < ITERATIONS:
        data = rb.read()
        if data is not None:
            count += 1
        else:
            # Busy spin
            pass
            
    end = time.perf_counter_ns()
    total_ns = end - start
    print(f"[Consumer] Recv {count} msgs. Total: {total_ns} ns")
    print(f"[Consumer] Throughput: {ITERATIONS / (total_ns/1e9):.2f} msgs/sec")
    print(f"[Consumer] Avg Latency (RTT-ish): {total_ns/ITERATIONS:.2f} ns")
    
    rb.close()
    rb.unlink()

if __name__ == "__main__":
    p = multiprocessing.Process(target=producer)
    c = multiprocessing.Process(target=consumer)
    
    c.start()
    p.start()
    
    c.join()
    p.join()
