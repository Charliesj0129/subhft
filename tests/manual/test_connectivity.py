import os
import sys
import time
from pathlib import Path


def load_env():
    env_path = Path(".env")
    if not env_path.exists():
        print("❌ .env file not found!")
        return False

    print("✅ .env found. Loading credentials...")
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")
    return True


sys.path.append(str(Path.cwd() / "src"))

try:
    import shioaji as sj
except ImportError:
    print("❌ Shioaji not installed!")
    sys.exit(1)


def run_connectivity_test():
    print("🚀 Starting Shioaji Connectivity Test...")

    if not load_env():
        return

    api_key = os.getenv("SHIOAJI_API_KEY")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY")

    if not api_key or not secret_key:
        print("❌ Missing SHIOAJI_API_KEY or SHIOAJI_SECRET_KEY in .env")
        return

    api = sj.Shioaji()

    print("🔑 Logging in with API key...")
    try:
        api.login(api_key=api_key, secret_key=secret_key)
        print("✅ Login Successful!")
    except Exception as e:
        print(f"❌ Login Failed: {e}")
        return

    print("📜 Fetching Contracts (Stocks)...")
    target_code = "2330"
    contract = None
    try:
        contract = api.Contracts.Stocks.TSE[target_code]
        print(f"✅ Found Contract: {contract.name} ({contract.code})")
    except Exception as e:
        print(f"❌ Failed to find contract {target_code}: {e}")
        return

    print("📡 Subscribing to Ticks & BidAsk...")

    tick_count = 0
    bidask_count = 0

    def on_tick(exchange, tick):
        nonlocal tick_count
        tick_count += 1
        print(f"   Tick: {tick.close} @ {tick.ts}")

    def on_bidask(exchange, bidask):
        nonlocal bidask_count
        bidask_count += 1
        print(f"   BidAsk: {bidask.bid_price[0]} / {bidask.ask_price[0]} @ {bidask.ts}")

    api.quote.set_on_tick_stk_v1_callback(on_tick)
    api.quote.set_on_bidask_stk_v1_callback(on_bidask)

    api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick)
    api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.BidAsk)

    print("⏳ Waiting for data (10s)...")
    time.sleep(10)

    print("-" * 30)
    print("📊 Results:")
    print(f"   Ticks received: {tick_count}")
    print(f"   BidAsks received: {bidask_count}")

    if tick_count > 0 or bidask_count > 0:
        print("✅ connectivity_verified: TRUE")
    else:
        print("⚠️ No data received (market might be closed or slow).")
        print("   connectivity_verified: PARTIAL (Login OK, Data stream silent)")

    api.logout()
    print("👋 Logged out.")


if __name__ == "__main__":
    run_connectivity_test()
