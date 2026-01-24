import sys
import os
import argparse
import numpy as np
from importlib.util import spec_from_file_location, module_from_spec

# Ensure hftbacktest is in path
HFTBACKTEST_PATH = "/home/charlie/hft_platform/external_repos/hftbacktest_fresh/py-hftbacktest"
if HFTBACKTEST_PATH not in sys.path:
    sys.path.append(HFTBACKTEST_PATH)

from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest

def load_strategy(strategy_path):
    name = os.path.basename(strategy_path).replace(".py", "")
    spec = spec_from_file_location(name, strategy_path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "strategy"):
        raise ValueError(f"Strategy file {strategy_path} must define a 'strategy(hbt)' function.")
    return module.strategy

def run(args):
    print(f"Running backtest with strategy: {args.strategy}")
    print(f"Data directory: {args.data_dir}")
    
    # Load Strategy
    strategy_fn = load_strategy(args.strategy)
    
    # Setup Asset
    # In a real scenario, these paths would be dynamic or config-based
    # For now, we assume standard naming or args
    
    # Example setup (simplified)
    # You might want to make this configurable via JSON or YAML
    asset = (
        BacktestAsset()
           # Example data loading - user needs to ensure these files exist
            .data([os.path.join(args.data_dir, f) for f in os.listdir(args.data_dir) if f.endswith('.npz')])
            .linear_asset(1.0)
            .tick_size(0.1) # Should be configurable
            .lot_size(0.001) # Should be configurable
    )
    
    backtest = HashMapMarketDepthBacktest([asset])
    
    print("Starting simulation...")
    strategy_fn(backtest)
    print("Simulation complete.")
    
    # Stats (if available)
    # create stats report...

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", required=True, help="Path to strategy python file")
    parser.add_argument("--data-dir", required=True, help="Directory containing .npz data files")
    args = parser.parse_args()
    
    run(args)
