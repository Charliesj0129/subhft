"""CLI commands: resolve-symbols, symbols build/preview/validate/sync."""

from __future__ import annotations

import argparse
import os
import sys

from ._utils import _print_issues

# Loop_v1 L2: subscription-limit defaults are loop-aware.
# A bound loop (--loop r47_tmf_v1) caps the subscription universe at the
# live-minimal size; legacy multi-broker, multi-symbol research runs keep the
# 480 ceiling (4 conn × 120 codes).
_MAX_SUBS_LOOP = 8
_MAX_SUBS_LEGACY = 480


def _resolve_max_subscriptions(args: argparse.Namespace) -> int:
    """Loop-aware default: 8 when --loop is set, otherwise 480.

    Respects an explicit user-provided ``--max-subscriptions`` value when it
    isn't ``None``. Reads ``loop_id`` defensively so handlers stay safe even
    if a future subparser drops the flag.
    """
    explicit = getattr(args, "max_subscriptions", None)
    if explicit is not None:
        return int(explicit)
    if getattr(args, "loop_id", None):
        return _MAX_SUBS_LOOP
    return _MAX_SUBS_LEGACY


def _resolve_symbols_shioaji(args: argparse.Namespace) -> None:
    """Resolve TSE/OTC exchanges via Shioaji broker."""

    from hft_platform.feed_adapter.contract_fetcher import resolve_symbol_exchanges

    symbols = args.symbols
    if not symbols:
        print("No symbols provided via args, please provide list.")
        sys.exit(1)

    try:
        result = resolve_symbol_exchanges(symbols)
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)

    for code in symbols:
        if not any(r["code"] == code for r in result):
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
    validation = validate_symbols(result.symbols, contract_index, max_subscriptions=_resolve_max_subscriptions(args))

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
    validation = validate_symbols(result.symbols, contract_index, max_subscriptions=_resolve_max_subscriptions(args))

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

    validation = validate_symbols(symbols, contract_index, max_subscriptions=_resolve_max_subscriptions(args))

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
    validation = validate_symbols(result.symbols, contract_index, max_subscriptions=_resolve_max_subscriptions(args))

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
