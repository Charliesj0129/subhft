
import sys
import os
import asyncio
from typing import Dict, Any, List
# from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest
# Assuming hftbacktest API usage. For this prototype, we'll mock the loop if deps missing.
from structlog import get_logger

logger = get_logger("backtest")


from dataclasses import dataclass, field

@dataclass
class HftBacktestConfig:
    data: List[str]
    symbols: List[str] = field(default_factory=list)
    tick_sizes: List[float] = field(default_factory=list)
    lot_sizes: List[float] = field(default_factory=list)
    latency_entry: float = 0
    latency_resp: float = 0
    fee_maker: float = 0
    fee_taker: float = 0
    partial_fill: bool = True
    record_out: str = None
    report: bool = False

class HftBacktestRunner:
    def __init__(self, cfg: HftBacktestConfig):
        self.cfg = cfg
        self.strategy_name = "demo" # todo: extract from args or cfg if added
        self.date = "20241215"
        self.symbol = cfg.symbols[0] if cfg.symbols else "2330"
        self.strategy_instance = None
        
    def run(self):
        logger.info("Initializing Backtest", symbol=self.symbol)
        
        # ... (rest of logic needs adaptation to use cfg.data etc)
        # 1. Load Strategy Class
        # Similar logic to manage.py run_strategy
        try:
             import importlib
             # Dynamically load from package
             mod = importlib.import_module(f"hft_platform.strategies.{self.strategy_name}")
             # Find class
             # Naming convention: snake_case strategy file -> PascalCase class?
             # Or inspect module for BaseStrategy subclass
             from hft_platform.strategy.base import BaseStrategy
             
             target_cls = None
             for name, obj in vars(mod).items():
                 if isinstance(obj, type) and issubclass(obj, BaseStrategy) and obj is not BaseStrategy:
                     target_cls = obj
                     break
            
             if not target_cls:
                 raise ValueError("No BaseStrategy subclass found")
                 
             self.strategy_instance = target_cls(strategy_id=self.strategy_name)
             logger.info("Loaded Strategy", class_name=target_cls.__name__)

        except Exception as e:
            logger.error("Failed to load strategy", error=str(e))
            return

        # 2. Prepare Data
        # For this demo, we check if local npz exists, otherwise generate/download mock
        data_path = f"data/{self.symbol}_{self.date}.npz"
        self._ensure_data(data_path)
        
        # 3. Execution using Adapter
        from hft_platform.backtest.adapter import HftBacktestAdapter
        
        try:
            adapter = HftBacktestAdapter(
                strategy=self.strategy_instance,
                asset_symbol=self.symbol,
                data_path=data_path
            )
            
            # Run
            result = adapter.run()
            
            # result is True if success? hftbacktest return bool usually.
            # Stats come from hbt.stats?
            # actually adapter.run returns hbt.close() which might return bool.
            
            # Inspect internal stats if available in wrapper
            # For now, we assume success
            logger.info("Simulation finished")
            
            # 4. Generate Report (Mock PnL for now)
            if self.cfg.report:
                self._generate_report(1234.5) 

        except ImportError as e:
            logger.error("HftBacktest not installed. Please install it.", error=str(e))
        except Exception as e:
            logger.error("Simulation failed", error=str(e))

    def _ensure_data(self, path):
         if not os.path.exists("data"):
             os.makedirs("data")
         if not os.path.exists(path):
             logger.info("Generating mock NPZ data for demo...", path=path)
             try:
                 # Check for hftbacktest helper
                 # New hftbacktest might not have generate_dummy_data exposed easily
                 # We'll create simple standard structure manually using numpy
                 import numpy as np
                 
                 # Structure: [event_flags, exch_ts, local_ts, price, qty]
                 # 1000 ticks
                 count = 1000
                 data = np.zeros(count, dtype=[
                     ('ev', 'u8'), 
                     ('exch_ts', 'u8'), 
                     ('local_ts', 'u8'), 
                     ('price', 'f8'), 
                     ('qty', 'f8')
                 ])
                 
                 start_ts = 1600000000000000 # arbitrary
                 
                 # Random walk
                 price = 100.0
                 for i in range(count):
                     price += np.random.randn() * 0.05
                     data[i]['ev'] = 1 # Trade? 
                     data[i]['exch_ts'] = start_ts + i * 1000000 # 1ms
                     data[i]['local_ts'] = start_ts + i * 1000000 + 100
                     data[i]['price'] = price
                     data[i]['qty'] = 1.0
                 
                 # Save compressed
                 np.savez_compressed(path, data=data) 
                 logger.info("Generated mock data", count=count)
                 
             except ImportError:
                 logger.error("NumPy not installed, cannot generate mock data.")
             except Exception as e:
                 logger.error("Failed to generate data", error=str(e))

    def _generate_report(self, pnl):
        try:
            from hft_platform.backtest.reporting import HTMLReporter
            import numpy as np
            
            report_path = f"reports/{self.strategy_name}_{self.date}.html"
            if not os.path.exists("reports"):
                os.makedirs("reports")
                
            reporter = HTMLReporter(report_path)
            
            # TODO: Extract real equity curve from hftbacktest stats
            # For prototype demo, generating a synthetic curve based on final PnL
            # This allows the user to see the "Chart" features immediately
            steps = 1000
            start_equity = 1_000_000
            end_equity = start_equity + pnl
            
            # Random walk bridge
            equity_metrics = np.linspace(start_equity, end_equity, steps)
            noise = np.random.normal(0, (end_equity - start_equity) * 0.1, steps)
            equity_curve = equity_metrics + noise
            equity_curve[0] = start_equity
            equity_curve[-1] = end_equity
            
            # Access timestamps (mock)
            base_ts = 1600000000 * 1e9
            timestamps = np.linspace(base_ts, base_ts + 3600*1e9, steps)
            
            reporter.compute_stats(timestamps, equity_curve)
            reporter.generate()
            
            logger.info("Visual Report Generated", path=report_path)
            
        except Exception as e:
            logger.error("Failed to generate HTML report", error=str(e))
            # Fallback
            with open(f"reports/error_{self.date}.txt", "w") as f:
                f.write(f"Error generating report: {e}")

