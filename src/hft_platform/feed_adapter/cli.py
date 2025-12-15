import argparse
import yaml
import sys
from structlog import get_logger
from hft_platform.feed_adapter.shioaji_client import ShioajiClient

logger = get_logger("feed_cli")

def main():
    parser = argparse.ArgumentParser(description="Feed Adapter Tool")
    parser.add_argument("--config", default="config/symbols.yaml", help="Path to symbols config")
    parser.add_argument("command", choices=["status", "verify-login"], help="Command to run")
    
    args = parser.parse_args()
    
    if args.command == "status":
        print(f"Loading config from {args.config}...")
        try:
            with open(args.config, "r") as f:
                data = yaml.safe_load(f)
                symbols = data.get("symbols", [])
                print(f"Configured Symbols: {len(symbols)}")
                for s in symbols[:5]:
                    print(f" - {s}")
                if len(symbols) > 5:
                    print(" ...")
        except Exception as e:
            print(f"Error loading config: {e}")
            sys.exit(1)

    elif args.command == "verify-login":
        client = ShioajiClient(args.config)
        try:
            client.login()
            print("Login Successful!")
        except Exception as e:
            print(f"Login Failed: {e}")
            sys.exit(1)

if __name__ == "__main__":
    main()
