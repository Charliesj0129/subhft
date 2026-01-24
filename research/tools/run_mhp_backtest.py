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
    print(f"Running MHP backtest with strategy: {args.strategy}")
    
    strategy_fn = load_strategy(args.strategy)
    
    # Needs Multi-Asset Setup: TXF (asset 0) and MXF (asset 1)
    
    # Asset 0: TXF
    asset0 = (
        BacktestAsset()
            .data(['data/synthetic_txf.npz'])
            .linear_asset(1.0)
            .tick_size(1.0)
            .lot_size(1.0)
            .constant_latency(0, 0)
            .risk_adverse_queue_model()
    )
    
    # Asset 1: MXF
    asset1 = (
        BacktestAsset()
            .data(['data/synthetic_mxf.npz'])
            .linear_asset(1.0)
            .tick_size(1.0)
            .lot_size(1.0)
            .constant_latency(0, 0)
            .risk_adverse_queue_model()
    )
    
    print("Initializing Multi-Asset Backtester...")
    # List order matters! 0=TXF, 1=MXF
    backtest = HashMapMarketDepthBacktest([asset0, asset1])
    
    print("Starting simulation...")
    strategy_fn(backtest)
    print("Simulation complete.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", required=True, help="Path to strategy python file")
    args = parser.parse_args()
    run(args)
