import argparse
import asyncio
import yaml
from hft_platform.backtest.config import BacktestConfig
from hft_platform.backtest.runner import BacktestRunner
from hft_platform.utils.logging import configure_logging

def main():
    configure_logging()
    parser = argparse.ArgumentParser(description="HFT Backtest CLI")
    parser.add_argument("--config", type=str, required=True, help="Path to backtest config yaml")
    args = parser.parse_args()
    
    with open(args.config, "r") as f:
        raw_cfg = yaml.safe_load(f)
        
    # Convert to DC (simplified)
    cfg = BacktestConfig(
        run_name=raw_cfg["run_name"],
        strategy_name=raw_cfg["strategy_name"],
        strategy_config_path=raw_cfg["strategy_config_path"],
        symbols=raw_cfg["symbols"],
        start_date=raw_cfg["start_date"],
        end_date=raw_cfg["end_date"]
    )
    
    runner = BacktestRunner(cfg)
    asyncio.run(runner.run())

if __name__ == "__main__":
    main()
