"""Backtest CLI commands (convert / run)."""

from __future__ import annotations

import argparse
import sys


def cmd_backtest(args: argparse.Namespace) -> None:
    if args.backtest_cmd is None:
        print("Please specify backtest subcommand (convert|run)")
        sys.exit(1)

    if args.backtest_cmd == "convert":
        try:
            from hft_platform.backtest.convert import convert_jsonl_to_npz

            convert_jsonl_to_npz(args.input, args.output, scale=args.scale)
            print(f"Converted to {args.output}")
        except Exception as exc:
            print(f"Convert failed: {exc}")
            sys.exit(1)
        return

    if args.backtest_cmd == "run":
        try:
            from hft_platform.backtest.adapter import StrategyHbtAdapter
            from hft_platform.backtest.runner import HftBacktestConfig, HftBacktestRunner
        except Exception as exc:
            print(f"Failed to import backtest modules: {exc}")
            sys.exit(1)

        # If strategy provided, use adapter; else simple buy-hold runner.
        if args.strategy_module:
            if len(args.data) > 1:
                print("Strategy bridge currently supports single-asset backtest; provide one data file.")
                sys.exit(1)
            adapter = StrategyHbtAdapter(
                data_path=args.data[0],
                strategy_module=args.strategy_module,
                strategy_class=args.strategy_class or "SimpleMarketMaker",
                strategy_id=args.strategy_id or "demo",
                symbol=args.symbol or "2330",
                tick_size=args.tick_size,
                lot_size=args.lot_size,
                maker_fee=(args.fee_maker or 0.0),
                taker_fee=(args.fee_taker or 0.0),
                partial_fill=not args.no_partial_fill,
                price_scale=args.price_scale,
                timeout=args.timeout,
                seed=int(args.seed),
            )
            adapter.run()
            print("Strategy backtest completed.")
        else:
            if len(args.data) != 1:
                print("Backtest runner currently supports one data file; run one symbol/file per invocation.")
                sys.exit(1)
            cfg = HftBacktestConfig(
                data=args.data,
                symbols=args.symbols,
                tick_sizes=args.tick_sizes or [args.tick_size],
                lot_sizes=args.lot_sizes or [args.lot_size],
                latency_entry=args.latency_entry,
                latency_resp=args.latency_resp,
                fee_maker=args.fee_maker,
                fee_taker=args.fee_taker,
                partial_fill=not args.no_partial_fill,
                strict_equity=bool(args.strict_equity),
                record_out=args.record_out,
                report=args.report,
                seed=int(args.seed),
            )
            runner = HftBacktestRunner(cfg)
            result = runner.run()
            if result is None:
                print("Backtest failed.")
                sys.exit(1)
            print(
                "Backtest completed.",
                f"run_id={result.run_id}",
                f"config={result.config_hash}",
                f"pnl={result.pnl:.2f}",
                f"synthetic={result.used_synthetic_equity}",
                f"equity_points={result.equity_points}",
            )
