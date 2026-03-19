"""CLI commands: resolve-symbols, symbols build/preview/validate/sync."""

from __future__ import annotations

import argparse
import os
import sys

from ._utils import _print_issues


def _resolve_symbols_shioaji(args: argparse.Namespace) -> None:
    """Resolve TSE/OTC exchanges via Shioaji broker."""

    try:
        import shioaji as sj
    except ImportError:
        print("Error: shioaji not installed. pip install shioaji")
        sys.exit(1)

    api_key = os.environ.get("SHIOAJI_API_KEY")
    secret_key = os.environ.get("SHIOAJI_SECRET_KEY")

    if not api_key or not secret_key:
        print("Error: SHIOAJI_API_KEY and SHIOAJI_SECRET_KEY env vars required.")
        sys.exit(1)

    print("Initializing Shioaji (Simulation mode)...")
    api = sj.Shioaji(simulation=True)
    try:
        api.login(api_key=api_key, secret_key=secret_key, contracts_timeout=60000)
    except Exception as e:
        print(f"Login failed: {e}")
        sys.exit(1)

    symbols = args.symbols
    if not symbols:
        print("No symbols provided via args, please provide list.")
        sys.exit(1)

    print("Building contract map...")
    code_map: dict[str, str] = {}
    try:
        for c in api.Contracts.Stocks.TSE:
            code_map[c.code] = "TSE"
        for c in api.Contracts.Stocks.OTC:
            code_map[c.code] = "OTC"
    except Exception as e:
        print(f"Contract fetch warning: {e}")

    result = []
    for code in symbols:
        exch = code_map.get(code)
        if exch:
            result.append({"code": code, "exchange": exch})
        else:
            print(f"Warning: {code} not found in TSE/OTC contracts.")

    output_data = {"symbols": result}

    if args.output:
        import yaml as _yaml

        with open(args.output, "w") as f:
            _yaml.dump(output_data, f, sort_keys=False)
        print(f"Written to {args.output}")
    else:
        import yaml as _yaml

        print(_yaml.dump(output_data, sort_keys=False))


def cmd_resolve_symbols(args: argparse.Namespace) -> None:
    """Resolve TSE/OTC exchanges for a list of symbols (broker-agnostic)."""
    broker = os.getenv("HFT_BROKER", "shioaji")

    if broker == "shioaji":
        _resolve_symbols_shioaji(args)
    elif broker == "fubon":
        print("Fubon resolve-symbols not yet implemented.")
    else:
        print(f"Error: unknown broker '{broker}'")
        sys.exit(1)


# Backward-compat alias: old monolithic cli.py had this as a standalone helper.
_resolve_symbols_shioaji = cmd_resolve_symbols


def cmd_symbols_build(args: argparse.Namespace) -> None:
    from hft_platform.config.symbols import (
        build_symbols,
        load_contract_cache,
        preview_lines,
        validate_symbols,
        write_symbols_yaml,
    )

    contract_index = None if args.no_contracts else load_contract_cache(args.contracts, args.metrics)
    result = build_symbols(args.list_path, contract_index)
    validation = validate_symbols(result.symbols, contract_index, max_subscriptions=args.max_subscriptions)

    errors = result.errors + validation.errors
    warnings = result.warnings + validation.warnings

    if args.preview:
        for line in preview_lines(result, sample=args.sample):
            print(line)

    if warnings or errors:
        _print_issues(errors, warnings)

    if errors:
        sys.exit(1)

    write_symbols_yaml(result.symbols, args.output)
    print(f"Written {len(result.symbols)} symbols to {args.output}")


def cmd_symbols_preview(args: argparse.Namespace) -> None:
    from hft_platform.config.symbols import build_symbols, load_contract_cache, preview_lines, validate_symbols

    contract_index = None if args.no_contracts else load_contract_cache(args.contracts, args.metrics)
    result = build_symbols(args.list_path, contract_index)
    validation = validate_symbols(result.symbols, contract_index, max_subscriptions=args.max_subscriptions)

    for line in preview_lines(result, sample=args.sample):
        print(line)

    errors = result.errors + validation.errors
    warnings = result.warnings + validation.warnings
    if warnings or errors:
        _print_issues(errors, warnings)

    if errors:
        sys.exit(1)


def cmd_symbols_validate(args: argparse.Namespace) -> None:
    from hft_platform.config.symbols import (
        ContractIndex,
        build_symbols,
        fetch_contracts_from_broker,
        load_contract_cache,
        load_metrics_cache,
        validate_symbols,
    )

    contract_index = None
    if args.online:
        contracts = fetch_contracts_from_broker()
        metrics = load_metrics_cache(args.metrics) if args.metrics else {}
        contract_index = ContractIndex(contracts=contracts, metrics_by_code=metrics)
    elif not args.no_contracts:
        contract_index = load_contract_cache(args.contracts, args.metrics)

    if args.symbols_path:
        import yaml

        with open(args.symbols_path, "r") as f:
            data = yaml.safe_load(f) or {}
        symbols = data.get("symbols", [])
    else:
        result = build_symbols(args.list_path, contract_index)
        symbols = result.symbols
        if result.errors:
            _print_issues(result.errors, result.warnings)
            sys.exit(1)

    validation = validate_symbols(symbols, contract_index, max_subscriptions=args.max_subscriptions)

    if validation.errors or validation.warnings:
        _print_issues(validation.errors, validation.warnings)

    if validation.errors:
        sys.exit(1)

    print("Configuration is valid.")


def cmd_symbols_sync(args: argparse.Namespace) -> None:
    from hft_platform.config.symbols import (
        ContractIndex,
        build_symbols,
        fetch_contracts_from_broker,
        load_metrics_cache,
        preview_lines,
        validate_symbols,
        write_contract_cache,
        write_symbols_yaml,
    )

    contracts = fetch_contracts_from_broker()
    write_contract_cache(contracts, args.contracts)
    metrics = load_metrics_cache(args.metrics) if args.metrics else {}
    contract_index = ContractIndex(contracts=contracts, metrics_by_code=metrics)

    result = build_symbols(args.list_path, contract_index)
    validation = validate_symbols(result.symbols, contract_index, max_subscriptions=args.max_subscriptions)

    errors = result.errors + validation.errors
    warnings = result.warnings + validation.warnings

    if args.preview:
        for line in preview_lines(result, sample=args.sample):
            print(line)

    if warnings or errors:
        _print_issues(errors, warnings)

    if errors:
        sys.exit(1)

    write_symbols_yaml(result.symbols, args.output)
    print(f"Written {len(result.symbols)} symbols to {args.output}")
    print(f"Contract cache saved to {args.contracts}")
