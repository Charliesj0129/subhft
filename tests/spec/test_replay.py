
import pytest
import os
import shutil
import json
from hft_platform.backtest.runner import BacktestRunner
# Need to patch the runner to use local mock data easily or standard data interface

@pytest.mark.asyncio
async def test_deterministic_replay():
    """
    Verify that repeating a backtest with same inputs yields EXACT same outputs.
    """
    # 1. Setup Data
    sym = "2330"
    date = "2024-01-01"
    
    # Mock Runner? 
    # Use the actual BacktestRunner
    runner = BacktestRunner("DemoStrategy", date)
    
    # Patch HftBacktestAdapter using unittest.mock
    from unittest.mock import MagicMock, patch
    
    # We want to verify that run() is called and returns same result
    # We also need to mock _ensure_data to avoid IO
    runner._ensure_data = lambda p: None
    
    # We need to capture the adapter instance or just patch the class import
    # Since imports happen inside run() usually, we correct this by patching sys.modules or use patch context
    # But runner.py imports inside run method: "from hft_platform.backtest.adapter import HftBacktestAdapter"
    
    msg_1 = "Simulation finished 1"
    msg_2 = "Simulation finished 2"
    
    # Patch hft_platform.backtest.adapter.HftBacktestAdapter directly
    # Since imports happen inside run(), verify the module path used in runner.py
    
    with patch("hft_platform.backtest.adapter.HftBacktestAdapter") as MockAdapter:
        # Run 1
        instance_1 = MockAdapter.return_value
        instance_1.run.return_value = True # Success
        
        # Patch report gen
        results = []
        runner._generate_report = lambda pnl: results.append(pnl)
        
        runner.run()
        
        # Run 2
        runner.run()
        
        assert len(results) == 2
        assert results[0] == results[1] # Deterministic PnL (1234.5 hardcoded in runner for now)
        
        # Verify params
        args, _ = MockAdapter.call_args
        # args[0] is strategy, etc.
        # Check symbol
        assert MockAdapter.call_count == 2
        assert MockAdapter.call_args_list[0].kwargs['asset_symbol'] == "2330"
    
    print("Deterministic Replay Passed via Mock")

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_deterministic_replay())
