import sys
import argparse
import importlib

def main():
    parser = argparse.ArgumentParser(description="HFT Platform CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Command: Monitor Latency
    parser_lat = subparsers.add_parser("monitor-latency", help="Trace eBPF latency")
    parser_lat.add_argument("--symbol", required=True, help="Function symbol to trace")
    parser_lat.add_argument("--lib", required=True, help="Library path")

    # Command: Subscribe Futures
    parser_sub = subparsers.add_parser("subscribe", help="Subscribe to Shioaji market data")
    
    # Command: Generate Config
    parser_gen = subparsers.add_parser("generate-config", help="Generate mass configuration")

    args, unknown = parser.parse_known_args()

    if args.command == "monitor-latency":
        # Dynamic import to avoid heavy deps if not needed
        from hft_platform.scripts import monitor_latency
        # Inject args into sys.argv or call function directly if refactored
        # For now, we just pass control (simplification)
        sys.argv = [sys.argv[0]] + unknown + ["--symbol", args.symbol, "--lib", args.lib]
        monitor_latency.main()
        
    elif args.command == "subscribe":
        from hft_platform.scripts import subscribe_futures
        subscribe_futures.main()
        
    elif args.command == "generate-config":
        from hft_platform.scripts import generate_mass_config_sharded
        generate_mass_config_sharded.main()
        
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
