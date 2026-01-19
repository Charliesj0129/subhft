import argparse
import json

from structlog import get_logger

from hft_platform.strategy.registry import StrategyRegistry

logger = get_logger("strategy.cli")


def cmd_list(args):
    registry = StrategyRegistry(args.config)
    for cfg in registry.configs:
        print(
            json.dumps(
                {
                    "id": cfg.strategy_id,
                    "module": cfg.module,
                    "class": cfg.class_name,
                    "enabled": cfg.enabled,
                    "budget_us": cfg.budget_us,
                    "symbols": cfg.symbols,
                }
            )
        )


def cmd_enable_disable(args, enabled: bool):
    registry = StrategyRegistry(args.config)
    updated = False
    for cfg in registry.configs:
        if cfg.strategy_id == args.id:
            cfg.enabled = enabled
            updated = True
    if not updated:
        logger.error("Strategy not found", id=args.id)
        return
    # write back
    try:
        import yaml

        data = {"strategies": [cfg.__dict__ for cfg in registry.configs]}
        with open(args.config, "w") as f:
            yaml.safe_dump(data, f)
        logger.info("Updated strategy", id=args.id, enabled=enabled)
    except Exception as exc:
        logger.error("Failed to update strategy config", error=str(exc))


def main():
    parser = argparse.ArgumentParser(description="Strategy control CLI")
    parser.add_argument("--config", default="config/strategies.yaml", help="Strategy config path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub_list = sub.add_parser("list", help="List strategies")
    sub_list.set_defaults(func=cmd_list)

    sub_en = sub.add_parser("enable", help="Enable strategy")
    sub_en.add_argument("id")
    sub_en.set_defaults(func=lambda args: cmd_enable_disable(args, True))

    sub_dis = sub.add_parser("disable", help="Disable strategy")
    sub_dis.add_argument("id")
    sub_dis.set_defaults(func=lambda args: cmd_enable_disable(args, False))

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
