"""Fubon symbol list manager and exchange mapper.

Unlike Shioaji which exposes hierarchical ``Contracts.Stocks.TSE`` objects,
Fubon uses plain string symbol codes.  This runtime reads symbols from the
platform config (``symbols.yaml`` or a config dict) and provides exchange
lookups, format validation, and hot-reload with diff logging.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from structlog import get_logger

logger = get_logger("feed_adapter.fubon.contracts_runtime")

# TWSE/OTC stock codes: 4-6 alphanumeric characters.
# Futures/options codes are longer but still alphanumeric.
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")


class FubonContractsRuntime:
    """Symbol list manager and exchange mapper for Fubon broker."""

    __slots__ = ("_sdk", "_symbols", "_code_exchange_map", "_config_path", "log")

    def __init__(
        self,
        sdk: Any,
        config_path: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self._sdk = sdk
        self._config_path = config_path
        self._symbols: list[dict[str, Any]] = []
        self._code_exchange_map: dict[str, str] = {}
        self.log = logger
        self._load_symbols(config)

    # ------------------------------------------------------------------ #
    # Symbol loading
    # ------------------------------------------------------------------ #

    def _load_symbols(self, config: dict[str, Any] | None = None) -> None:
        """Populate ``_symbols`` and ``_code_exchange_map`` from config.

        Resolution order:
        1. YAML file at ``_config_path`` (if provided and readable).
        2. ``config["symbols"]`` list (if provided).
        3. Empty list (logged as warning).
        """
        symbols: list[dict[str, Any]] = []

        # Try YAML file first.
        if self._config_path:
            symbols = self._read_yaml(self._config_path)

        # Fall back to config dict.
        if not symbols and config is not None:
            raw = config.get("symbols")
            if isinstance(raw, list):
                symbols = list(raw)

        if not symbols:
            self.log.warning("No symbols loaded for Fubon contracts runtime")

        self._symbols = symbols
        self._code_exchange_map = {
            str(s["code"]): str(s.get("exchange", ""))
            for s in symbols
            if s.get("code") is not None
        }
        self.log.info(
            "Fubon symbols loaded",
            symbol_count=len(self._symbols),
            exchange_count=len(self._code_exchange_map),
        )

    @staticmethod
    def _read_yaml(path: str) -> list[dict[str, Any]]:
        """Read a symbols YAML file and return the ``symbols`` list."""
        try:
            import yaml  # noqa: WPS433 — lazy import to keep module lightweight
        except ImportError:
            logger.warning("PyYAML not installed; cannot read symbols YAML")
            return []

        p = Path(path)
        if not p.is_file():
            logger.debug("Symbols YAML not found", path=path)
            return []

        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to parse symbols YAML", path=path, error=str(exc))
            return []

        if isinstance(data, dict):
            raw = data.get("symbols")
            if isinstance(raw, list):
                return raw
        return []

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @property
    def symbols(self) -> list[dict[str, Any]]:
        """Return the current symbol list."""
        return self._symbols

    def validate_symbols(self) -> list[str]:
        """Return a list of symbol codes that fail format validation.

        Valid codes are 1-20 alphanumeric characters.
        """
        invalid: list[str] = []
        for sym in self._symbols:
            code = sym.get("code")
            if code is None:
                continue
            code_str = str(code)
            if not _SYMBOL_RE.match(code_str):
                invalid.append(code_str)
        if invalid:
            self.log.warning(
                "Invalid symbol codes detected",
                count=len(invalid),
                samples=invalid[:10],
            )
        return invalid

    def get_exchange(self, symbol: str) -> str:
        """Look up the exchange for *symbol*.  Returns ``""`` if unknown."""
        return self._code_exchange_map.get(symbol, "")

    def reload_symbols(self, config: dict[str, Any] | None = None) -> None:
        """Re-read config, rebuild maps, and log added/removed symbols."""
        old_codes = set(self._code_exchange_map)
        self._load_symbols(config)
        new_codes = set(self._code_exchange_map)

        added = sorted(new_codes - old_codes)
        removed = sorted(old_codes - new_codes)

        if added or removed:
            self.log.info(
                "Fubon symbols reloaded with changes",
                added_count=len(added),
                removed_count=len(removed),
                added=added[:20],
                removed=removed[:20],
            )
        else:
            self.log.info("Fubon symbols reloaded, no changes")

    def refresh_status(self) -> dict[str, Any]:
        """Return a summary dict describing the current symbol state."""
        return {
            "status": "ok",
            "source": "yaml" if self._config_path else "config",
            "symbol_count": len(self._symbols),
        }
