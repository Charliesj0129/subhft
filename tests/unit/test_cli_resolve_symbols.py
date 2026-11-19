"""Tests for broker-agnostic cmd_resolve_symbols in cli.py."""

from __future__ import annotations

import argparse
from unittest import mock

import pytest


def _make_args(symbols: list[str] | None = None, output: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(symbols=symbols or ["2330"], output=output)


class TestCmdResolveSymbolsBrokerDispatch:
    """Verify broker dispatch logic in cmd_resolve_symbols."""

    def test_shioaji_import_error_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With HFT_BROKER=shioaji and shioaji not installed, exit gracefully."""
        monkeypatch.setenv("HFT_BROKER", "shioaji")

        def _shioaji_import_fail(_args: object) -> None:
            raise SystemExit(1)

        with mock.patch("hft_platform.cli._symbols._resolve_symbols_shioaji", side_effect=_shioaji_import_fail):
            from hft_platform.cli import cmd_resolve_symbols

            with pytest.raises(SystemExit):
                cmd_resolve_symbols(_make_args())

    def test_fubon_not_yet_implemented(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With HFT_BROKER=fubon, function returns without error (info log only)."""
        monkeypatch.setenv("HFT_BROKER", "fubon")
        from hft_platform.cli import cmd_resolve_symbols

        assert cmd_resolve_symbols(_make_args()) is None

    def test_unknown_broker_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With HFT_BROKER=unknown, exit with error."""
        monkeypatch.setenv("HFT_BROKER", "unknown_broker")
        from hft_platform.cli import cmd_resolve_symbols

        with pytest.raises(SystemExit):
            cmd_resolve_symbols(_make_args())

    def test_default_broker_is_shioaji(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When HFT_BROKER is not set, defaults to shioaji."""
        monkeypatch.delenv("HFT_BROKER", raising=False)
        with mock.patch("hft_platform.cli._symbols._resolve_symbols_shioaji") as mock_shioaji:
            from hft_platform.cli import cmd_resolve_symbols

            args = _make_args()
            cmd_resolve_symbols(args)
            mock_shioaji.assert_called_once_with(args)
