"""Fubon symbol validation, exchange lookup, and hot-reload runtime.

Mirrors the Shioaji ContractsRuntime pattern but adapted for Fubon's
simpler contract model where symbols and exchanges come from YAML config
rather than a broker SDK contracts tree.

Design notes
------------
- **Allocator Law**: Exchange map is built once at init/reload time.
  No per-lookup allocations on the hot path.
- All timestamps use ``timebase.now_ns()`` per project convention.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("feed_adapter.fubon.contracts_runtime")

# Fubon market codes.
FUBON_EXCHANGES: frozenset[str] = frozenset({"TSE", "OTC", "ESB", "TIB", "TAIFEX"})

# Symbol format: 1-20 alphanumeric characters.
_SYMBOL_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9]{1,20}$")


class FubonContractsRuntime:
    """Symbol validation, exchange lookup, and hot-reload for Fubon."""

    __slots__ = (
        "_sdk",
        "_symbols_config",
        "_symbols_path",
        "_code_exchange_map",
        "_symbol_list",
        "_last_reload_ns",
    )

    def __init__(
        self,
        sdk: Any,
        symbols_config: dict[str, Any] | list[dict[str, Any]] | None = None,
        symbols_path: str | None = None,
    ) -> None:
        self._sdk = sdk
        self._symbols_path: str | None = symbols_path
        self._code_exchange_map: dict[str, str] = {}
        self._symbol_list: list[dict[str, Any]] = []
        self._last_reload_ns: int = 0

        # Resolve initial config: explicit config takes priority over file path.
        if symbols_config is not None:
            self._symbols_config = symbols_config
        elif symbols_path is not None:
            self._symbols_config = self._load_yaml(symbols_path)
        else:
            self._symbols_config = None

        self._apply_config(self._symbols_config)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @property
    def symbols(self) -> list[dict[str, Any]]:
        """Return the current symbol list (read-only copy)."""
        return list(self._symbol_list)

    def validate_symbols(self) -> list[str]:
        """Validate symbol format; return list of INVALID symbol codes."""
        invalid: list[str] = []
        for sym in self._symbol_list:
            code = sym.get("code", "")
            if not _SYMBOL_RE.match(str(code)):
                invalid.append(str(code))
        if invalid:
            logger.warning(
                "fubon_invalid_symbols",
                count=len(invalid),
                symbols=invalid[:10],
            )
        return invalid

    def get_exchange(self, symbol: str) -> str:
        """O(1) exchange lookup by symbol code. Returns '' if not found."""
        return self._code_exchange_map.get(symbol, "")

    def reload_symbols(self) -> None:
        """Re-read symbols config file (if path provided) and diff old vs new."""
        old_codes = set(self._code_exchange_map)

        if self._symbols_path is not None:
            self._symbols_config = self._load_yaml(self._symbols_path)

        self._apply_config(self._symbols_config)
        new_codes = set(self._code_exchange_map)

        added = sorted(new_codes - old_codes)
        removed = sorted(old_codes - new_codes)

        if added or removed:
            logger.info(
                "fubon_symbols_reloaded",
                added_count=len(added),
                removed_count=len(removed),
                added=added[:20],
                removed=removed[:20],
            )
        else:
            logger.info("fubon_symbols_reloaded", changed=False)

    def refresh_status(self) -> dict[str, Any]:
        """Return diagnostic status dict."""
        return {
            "symbol_count": len(self._symbol_list),
            "last_reload_ts": self._last_reload_ns,
            "exchange_map_size": len(self._code_exchange_map),
        }

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _apply_config(self, config: dict[str, Any] | list[dict[str, Any]] | None) -> None:
        """Parse config into symbol list and exchange map."""
        symbols = self._normalize_config(config)
        self._symbol_list = symbols
        self._code_exchange_map = self._build_exchange_map(symbols)
        self._last_reload_ns = timebase.now_ns()

    @staticmethod
    def _normalize_config(
        config: dict[str, Any] | list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        """Accept dict-with-symbols-key or flat list; return list of symbol dicts."""
        if config is None:
            return []
        if isinstance(config, list):
            return list(config)
        # dict form: expect a "symbols" key containing a list
        raw = config.get("symbols", [])
        if isinstance(raw, list):
            return list(raw)
        return []

    @staticmethod
    def _build_exchange_map(symbols: list[dict[str, Any]]) -> dict[str, str]:
        """Build code -> exchange mapping from symbol list."""
        mapping: dict[str, str] = {}
        for sym in symbols:
            code = sym.get("code")
            exchange = sym.get("exchange")
            if code and exchange:
                mapping[str(code)] = str(exchange)
        return mapping

    @staticmethod
    def _load_yaml(path: str) -> dict[str, Any] | list[dict[str, Any]]:
        """Lazy-load YAML file. Returns parsed content or empty dict on error."""
        import yaml  # noqa: PLC0415 — lazy import per convention

        try:
            text = Path(path).read_text(encoding="utf-8")
            data = yaml.safe_load(text)
            if data is None:
                return {}
            return data  # type: ignore[return-value]
        except Exception as exc:
            logger.error("fubon_symbols_yaml_load_failed", path=path, error=str(exc))
            return {}
