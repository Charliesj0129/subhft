"""The retired Rust parity defaults must not name a test file that does not exist."""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path

from hft_platform.alpha.promotion import PromotionConfig
from hft_platform.cli._parser import build_parser
from hft_platform.cli_args.research_pipeline import add_common_research_pipeline_args

REPO_ROOT = Path(__file__).resolve().parents[2]
DEST = "rust_parity_test_path"


def _names_missing_file(value: str) -> bool:
    return bool(value) and not (REPO_ROOT / value).exists()


def _parser_defaults(parser: argparse.ArgumentParser) -> list[str]:
    found: list[str] = []
    for action in parser._actions:
        if action.dest == DEST:
            found.append(action.default)
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                found.extend(_parser_defaults(child))
    return found


def test_promotion_config_rust_parity_default_names_no_missing_file() -> None:
    default = {f.name: f.default for f in dataclasses.fields(PromotionConfig)}[DEST]

    assert not _names_missing_file(default), default


def test_alpha_promote_cli_rust_parity_default_names_no_missing_file() -> None:
    defaults = _parser_defaults(build_parser())

    assert defaults, "alpha promote no longer registers --rust-parity-test-path"
    assert not [d for d in defaults if _names_missing_file(d)], defaults


def test_research_pipeline_rust_parity_default_names_no_missing_file() -> None:
    parser = argparse.ArgumentParser()
    add_common_research_pipeline_args(parser, strict=True, handler=lambda _args: None)

    assert parser.get_default(DEST) is not None
    assert not _names_missing_file(parser.get_default(DEST)), parser.get_default(DEST)
