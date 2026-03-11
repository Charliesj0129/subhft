"""Tests for broker-agnostic cmd_resolve_symbols in cli.py."""

from __future__ import annotations

import argparse
import sys
from unittest import mock

import pytest


def _make_args(symbols: list[str] | None = None, output: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(symbols=symbols or ["2330"], output=output)


class TestCmdResolveSymbolsBrokerDispatch:
    """Verify broker dispatch logic in cmd_resolve_symbols."""

    def test_shioaji_import_error_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With HFT_BROKER=shioaji and shioaji not installed, exit gracefully."""
        monkeypatch.setenv("HFT_BROKER", "shioaji")
        # Remove shioaji from sys.modules so the lazy import fails
        monkeypatch.delitem(sys.modules, "shioaji", raising=False)

        import builtins

        real_import = builtins.__import__

        def _block_shioaji(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
            if name == "shioaji":
                raise ImportError("no shioaji")
            return real_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=_block_shioaji):
            from hft_platform.cli import cmd_resolve_symbols

            with pytest.raises(SystemExit):
                cmd_resolve_symbols(_make_args())

    def test_fubon_not_yet_implemented(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With HFT_BROKER=fubon, function returns without error (info log only)."""
        monkeypatch.setenv("HFT_BROKER", "fubon")
        from hft_platform.cli import cmd_resolve_symbols

        # Should NOT raise or exit
        cmd_resolve_symbols(_make_args())

    def test_unknown_broker_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With HFT_BROKER=unknown, exit with error."""
        monkeypatch.setenv("HFT_BROKER", "unknown_broker")
        from hft_platform.cli import cmd_resolve_symbols

        with pytest.raises(SystemExit):
            cmd_resolve_symbols(_make_args())

    def test_default_broker_is_shioaji(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When HFT_BROKER is not set, defaults to shioaji."""
        monkeypatch.delenv("HFT_BROKER", raising=False)
        # Patch _resolve_symbols_shioaji to verify it's called
        with mock.patch("hft_platform.cli._resolve_symbols_shioaji") as mock_shioaji:
            from hft_platform.cli import cmd_resolve_symbols

            args = _make_args()
            cmd_resolve_symbols(args)
            mock_shioaji.assert_called_once_with(args)
