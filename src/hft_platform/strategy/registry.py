import importlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml
from structlog import get_logger

logger = get_logger("strategy.registry")


@dataclass
class StrategyConfig:
    strategy_id: str
    module: str
    class_name: str
    enabled: bool = True
    budget_us: int = 200
    symbols: Optional[List[str]] = None
    product_type: str = "STOCK"
    params: Dict[str, Any] = field(default_factory=dict)


class StrategyRegistry:
    def __init__(self, config_path: str = "config/base/strategies.yaml"):
        self.config_path = config_path
        self.configs: List[StrategyConfig] = []
        self.load()

    def load(self):
        self.configs.clear()
        try:
            with open(self.config_path, "r") as f:
                data = yaml.safe_load(f) or {}
            for entry in data.get("strategies", []):
                cfg = StrategyConfig(
                    strategy_id=entry["id"],
                    module=entry["module"],
                    class_name=entry["class"],
                    enabled=entry.get("enabled", True),
                    budget_us=int(entry.get("budget_us", 200)),
                    symbols=entry.get("symbols"),
                    product_type=entry.get("product_type", "STOCK"),
                    params=entry.get("params", {}) or {},
                )
                self.configs.append(cfg)
            logger.info("Loaded strategies", count=len(self.configs))
        except FileNotFoundError:
            logger.warning("Strategy config not found", path=self.config_path)
        except Exception as exc:
            logger.error("Failed to load strategy config", error=str(exc))

    def instantiate(self):
        strategies = []
        for cfg in self.configs:
            try:
                module = importlib.import_module(cfg.module)
                cls = getattr(module, cfg.class_name)
                strategy = cls(strategy_id=cfg.strategy_id, **(cfg.params or {}))
                strategy.enabled = cfg.enabled
                strategy.symbols = cfg.symbols
                strategy.product_type = cfg.product_type
                strategy.budget_us = cfg.budget_us
                strategies.append(strategy)
            except Exception as exc:
                logger.error("Failed to instantiate strategy", id=cfg.strategy_id, error=str(exc))
        return strategies
