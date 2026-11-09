"""A metadata reload on the roll worker thread must not poison loop-thread lookups.

2026-11-09: ``apply_runtime_overlay`` ran ``_load`` on the universe-roll worker
thread. ``_load`` emptied ``meta`` before parsing the file, an event-loop
``product_type("TMFK6")`` landed in that gap and cached ``""``, and every later
R47 order went down the untyped stock path until restart.

The window is made deterministic by blocking ``yaml.safe_load`` on an event, so
no test here sleeps.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from hft_platform.feed_adapter.normalizer import SymbolMetadata

_TMF = {
    "code": "TMFK6",
    "exchange": "FUT",
    "product_type": "future",
    "tick_size": 1,
    "price_scale": 10000,
    "point_value": 10,
}
_STOCK = {"code": "2330", "exchange": "TSE", "tick_size": 0.5}


def _write(path: Path, symbols: list[dict[str, Any]]) -> Path:
    path.write_text(yaml.safe_dump({"symbols": symbols}), encoding="utf-8")
    return path


class _BlockedLoad:
    """Hold ``yaml.safe_load`` open so a reader can run inside ``_load``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.in_window = threading.Event()
        self.release = threading.Event()
        real = yaml.safe_load

        def blocking(stream: Any) -> Any:
            self.in_window.set()
            assert self.release.wait(5), "test never released the load"
            return real(stream)

        monkeypatch.setattr(yaml, "safe_load", blocking)


def test_lookup_during_worker_reload_is_not_cached_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    meta = SymbolMetadata(str(_write(tmp_path / "symbols.yaml", [_TMF, _STOCK])))
    gate = _BlockedLoad(monkeypatch)
    worker = threading.Thread(target=meta.apply_runtime_overlay, args=([dict(_TMF, name="rolled")],))
    worker.start()
    try:
        assert gate.in_window.wait(5)
        # Event-loop reads while the worker is mid-parse.
        during = (meta.product_type("TMFK6"), meta.exchange("TMFK6"), meta.price_scale("2330"))
    finally:
        gate.release.set()
        worker.join(5)
    assert not worker.is_alive()

    assert during == ("future", "FUT", 2)
    assert meta.meta["TMFK6"]["name"] == "rolled"
    assert meta.product_type("TMFK6") == "future"
    assert meta.exchange("TMFK6") == "FUT"
    assert meta.price_scale("2330") == 2
    assert meta.exchange("2330") == "TSE"
    assert meta.product_type("2330") == "stock"


def test_lookup_miss_is_not_cached_so_a_later_entry_resolves(tmp_path: Path) -> None:
    meta = SymbolMetadata(str(_write(tmp_path / "symbols.yaml", [dict(_TMF, code="TMFR1")])))
    assert meta.product_type("TMFK6") == ""
    assert meta.exchange("TMFK6") == ""
    assert meta.price_scale("TMFK6") == SymbolMetadata.DEFAULT_SCALE

    meta.set_alias_map({"TMFR1": "TMFK6"})

    assert meta.product_type("TMFK6") == "future"
    assert meta.exchange("TMFK6") == "FUT"
    assert meta.price_scale("TMFK6") == 10000


def test_alias_actual_code_survives_a_reload(tmp_path: Path) -> None:
    meta = SymbolMetadata(str(_write(tmp_path / "symbols.yaml", [dict(_TMF, code="TMFR1")])))
    meta.set_alias_map({"TMFR1": "TMFK6"})

    meta.reload()

    assert meta.meta["TMFK6"]["code"] == "TMFR1"
    assert meta.product_type("TMFK6") == "future"
    assert meta.exchange("TMFK6") == "FUT"


def test_failed_reload_still_publishes_overlay_and_empty_caches(tmp_path: Path) -> None:
    path = _write(tmp_path / "symbols.yaml", [_STOCK])
    meta = SymbolMetadata(str(path))
    assert meta.exchange("2330") == "TSE"
    path.write_text("symbols: [ : not yaml", encoding="utf-8")

    meta.apply_runtime_overlay([_TMF])

    assert "2330" not in meta.meta
    assert meta.exchange("2330") == ""
    assert meta.product_type("TMFK6") == "future"


def test_alias_recorded_after_the_build_copy_survives_the_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta = SymbolMetadata(str(_write(tmp_path / "symbols.yaml", [dict(_TMF, code="TMFR1")])))
    real_copy = meta._copy_alias_entries
    calls = {"n": 0}

    def copy_then_alias_arrives(table: dict[str, dict[str, Any]]) -> None:
        real_copy(table)
        calls["n"] += 1
        if calls["n"] == 1:
            # Another thread's set_alias_map lands after the in-build copy and
            # writes into the table that is about to be replaced.
            meta.set_alias_map({"TMFR1": "TMFK6"})

    monkeypatch.setattr(meta, "_copy_alias_entries", copy_then_alias_arrives)

    meta.reload()

    assert meta.contract_multiplier("TMFK6") == 10
    assert meta.product_type("TMFK6") == "future"
    assert calls["n"] == 2


def test_loop_reload_skips_instead_of_waiting_on_a_roll_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _write(tmp_path / "symbols.yaml", [_STOCK])
    meta = SymbolMetadata(str(path))
    gate = _BlockedLoad(monkeypatch)
    worker = threading.Thread(target=meta.apply_runtime_overlay, args=([_TMF],))
    worker.start()
    try:
        assert gate.in_window.wait(5)
        stamp = os.path.getmtime(path) + 10
        os.utime(path, (stamp, stamp))
        skipped = meta.reload_if_changed()
    finally:
        gate.release.set()
        worker.join(5)
    assert not worker.is_alive()

    assert skipped is False
    assert meta.reload_if_changed() is True
    assert meta.product_type("TMFK6") == "future"
    assert meta.exchange("2330") == "TSE"
