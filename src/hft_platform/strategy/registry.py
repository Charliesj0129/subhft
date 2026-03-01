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
    symbol_tags: Optional[List[str]] = None
    product_type: str = "STOCK"
    params: Dict[str, Any] = field(default_factory=dict)
    required_feature_set_id: str | None = None
    required_feature_schema_version: int | None = None
    required_feature_profile_id: str | None = None
    required_feature_ids: list[str] = field(default_factory=list)
    optional_feature_ids: list[str] = field(default_factory=list)


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
                    symbol_tags=entry.get("symbol_tags"),
                    product_type=entry.get("product_type", "STOCK"),
                    params=entry.get("params", {}) or {},
                    required_feature_set_id=entry.get("required_feature_set_id"),
                    required_feature_schema_version=(
                        int(entry["required_feature_schema_version"])
                        if entry.get("required_feature_schema_version") is not None
                        else None
                    ),
                    required_feature_profile_id=entry.get("required_feature_profile_id"),
                    required_feature_ids=list(entry.get("required_feature_ids") or []),
                    optional_feature_ids=list(entry.get("optional_feature_ids") or []),
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
                if cfg.symbols is not None:
                    strategy.symbols = cfg.symbols
                strategy.symbol_tags = cfg.symbol_tags
                strategy.product_type = cfg.product_type
                strategy.budget_us = cfg.budget_us
                strategy.required_feature_set_id = cfg.required_feature_set_id
                strategy.required_feature_schema_version = cfg.required_feature_schema_version
                strategy.required_feature_profile_id = cfg.required_feature_profile_id
                strategy.required_feature_ids = list(cfg.required_feature_ids or [])
                strategy.optional_feature_ids = list(cfg.optional_feature_ids or [])
                strategies.append(strategy)
            except Exception as exc:
                logger.error("Failed to instantiate strategy", id=cfg.strategy_id, error=str(exc))
        return strategies
