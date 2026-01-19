import os

from hft_platform.config.loader import load_settings
from hft_platform.feed_adapter.normalizer import SymbolMetadata

print("--- Testing Config Loader ---")
settings, _ = load_settings()
print(f"Loaded Mode: {settings.get('mode')}")
print(f"Symbols Path: {settings.get('paths', {}).get('symbols')}")

if settings.get("mode") == "sim":
    print("SUCCESS: Default mode is sim")
else:
    print(f"FAILURE: Default mode is {settings.get('mode')}")

print("\n--- Testing SymbolMetadata ---")
try:
    meta = SymbolMetadata()  # Default path
    print(f"Loaded Meta Count: {len(meta.meta)}")
    if len(meta.meta) > 0:
        print("SUCCESS: SymbolMetadata loaded symbols")
    else:
        print("WARNING: SymbolMetadata loaded 0 symbols (config empty?)")
except Exception as e:
    print(f"FAILURE: SymbolMetadata crashed: {e}")

print("\n--- Testing Env Override ---")
os.environ["HFT_MODE"] = "live"
settings_env, _ = load_settings()
print(f"Env Mode: {settings_env.get('mode')}")
if settings_env.get("mode") == "live":
    print("SUCCESS: Env override works")
else:
    print("FAILURE: Env override failed")
