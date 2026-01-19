print("Startup...", flush=True)
try:
    print("Importing hft_platform...", flush=True)
    print("Success: hft_platform", flush=True)

    print("Importing strategy.runner...", flush=True)
    print("Success: strategy.runner", flush=True)
except Exception as e:
    print(f"IMPORT FAILED: {e}", flush=True)
import traceback

traceback.print_exc()
