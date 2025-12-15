from dataclasses import dataclass, field
from typing import List, Dict, Optional

@dataclass
class LatencyProfile:
    base_latency_ns: int = 2_000_000 # 2ms
    jitter_std_ns: int = 500_000     # 0.5ms
    
@dataclass
class SlippageProfile:
    model: str = "constant" # "constant", "linear_impact"
    param: float = 0.0      # e.g., 1 tick or 0.5 bps

@dataclass
class BacktestConfig:
    run_name: str
    strategy_name: str
    strategy_config_path: str
    
    symbols: List[str]
    start_date: str # YYYY-MM-DD
    end_date: str   # YYYY-MM-DD
    
    latency: LatencyProfile = field(default_factory=LatencyProfile)
    slippage: SlippageProfile = field(default_factory=SlippageProfile)
    
    initial_capital: float = 1_000_000.0
