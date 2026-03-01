import sys
import os
import time
from concurrent.futures import ProcessPoolExecutor

print("1. Starting debug script...", flush=True)

try:
    import numpy as np
    print(f"2. NumPy {np.__version__} imported", flush=True)
except ImportError as e:
    print(f"ERROR: NumPy import failed: {e}", flush=True)

try:
    sys.path.insert(0, os.getcwd())
    from factor_registry import FactorRegistry
    print("3. FactorRegistry imported", flush=True)
except ImportError as e:
    print(f"ERROR: FactorRegistry import failed: {e}", flush=True)

try:
    from hft_factor_strategy import run_factor_backtest
    print("4. hft_factor_strategy imported", flush=True)
except ImportError as e:
    print(f"ERROR: Strategy import failed: {e}", flush=True)

def worker(x):
    return x * x

if __name__ == "__main__":
    print("5. Testing ProcessPoolExecutor...", flush=True)
    with ProcessPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(worker, i) for i in range(5)]
        for f in results:
            print(f"Result: {f.result()}", flush=True)
    print("6. Done.", flush=True)
