"""A live strategy bound by contract family must not also pin a concrete expiry.

`StrategyRunner._apply_family_bindings` *unions* the resolved front-month code
into `strategy.symbols`, and `_on_family_rebind` discards only the previously
*resolved* ref. A concrete month written in `config/live/strategies.yaml` is
therefore never removed by any rollover: it stays in the set until someone edits
the file. On THESHOW that seed was `TMFE6` (May 2026) and it logged
`preflight_symbol_mismatch` at ERROR on every connect for months.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

STRATEGIES_PATH = Path(__file__).resolve().parents[2] / "config" / "live" / "strategies.yaml"

# TAIFEX monthly code: root + month letter (A-L) + single-digit year, e.g. TMFE6.
_CONCRETE_MONTH = re.compile(r"^[A-Z]{2,4}[A-L][0-9]$")


def _live_strategies() -> list[dict]:
    with STRATEGIES_PATH.open() as handle:
        data = yaml.safe_load(handle)
    return list(data.get("strategies") or [])


def test_family_bound_strategies_do_not_pin_a_concrete_expiry_month() -> None:
    offenders: list[tuple[str, list[str]]] = []
    for entry in _live_strategies():
        if not entry.get("contract_families"):
            continue
        pinned = [str(s) for s in (entry.get("symbols") or []) if _CONCRETE_MONTH.match(str(s))]
        if pinned:
            offenders.append((str(entry.get("id")), pinned))

    assert not offenders, (
        "These strategies declare contract_families and also pin concrete expiry months. "
        "The family binding unions its resolution into strategy.symbols and never removes "
        "a YAML seed, so the pinned code survives every rollover and fails preflight forever: "
        f"{offenders}"
    )


def test_every_live_strategy_can_still_resolve_symbols_from_somewhere() -> None:
    """Dropping a seed must not leave a strategy with no way to bind at all.

    An entry with neither `symbols` nor `contract_families` would load, run, and
    silently see no events -- the failure this file exists to prevent.
    """
    unbindable = [
        str(entry.get("id"))
        for entry in _live_strategies()
        if entry.get("enabled") and not entry.get("symbols") and not entry.get("contract_families")
    ]
    assert not unbindable, f"enabled strategies with no symbol source: {unbindable}"
