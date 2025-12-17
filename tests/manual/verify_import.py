
print("Startup...", flush=True)
try:
    print("Importing hft_platform...", flush=True)
    import hft_platform
    print("Success: hft_platform", flush=True)
    
    print("Importing strategy.runner...", flush=True)
    from hft_platform.strategy.runner import StrategyRunner
    print("Success: strategy.runner", flush=True)
except Exception as e:
    print(f"IMPORT FAILED: {e}", flush=True)
import traceback
traceback.print_exc()
