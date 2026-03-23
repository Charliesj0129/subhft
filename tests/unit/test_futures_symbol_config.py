"""Verify futures contracts have required trading parameters."""

from __future__ import annotations

from pathlib import Path

import yaml

SYMBOLS_PATH = Path(__file__).resolve().parents[2] / "config" / "symbols.yaml"

EXPECTED_FUTURES = {
    "TXF": {"point_value": 200, "tick_size": 1, "price_scale": 10000},
    "MXF": {"point_value": 50, "tick_size": 1, "price_scale": 10000},
    "TMF": {"point_value": 10, "tick_size": 1, "price_scale": 10000},
}


def _load_symbols() -> list[dict]:
    with open(SYMBOLS_PATH) as f:
        data = yaml.safe_load(f)
    # Handle both flat list and dict with 'symbols' key
    if isinstance(data, dict):
        return data.get("symbols", data.get("list", []))
    return data if isinstance(data, list) else []


def _futures_symbols() -> list[dict]:
    return [s for s in _load_symbols() if s.get("product_type") == "future"]


def test_futures_have_point_value() -> None:
    for sym in _futures_symbols():
        assert "point_value" in sym, f"{sym['code']} missing point_value"
        assert isinstance(sym["point_value"], int), f"{sym['code']} point_value must be int"
        assert sym["point_value"] > 0, f"{sym['code']} point_value must be positive"


def test_futures_have_tick_size() -> None:
    for sym in _futures_symbols():
        assert "tick_size" in sym, f"{sym['code']} missing tick_size"
        assert sym["tick_size"] > 0, f"{sym['code']} tick_size must be positive"


def test_futures_have_price_scale() -> None:
    for sym in _futures_symbols():
        assert "price_scale" in sym, f"{sym['code']} missing price_scale"
        assert sym["price_scale"] == 10000, f"{sym['code']} price_scale must be 10000"


def test_futures_point_values_correct() -> None:
    for sym in _futures_symbols():
        code = sym["code"]
        category = None
        for cat in EXPECTED_FUTURES:
            if code.startswith(cat):
                category = cat
                break
        if category is None:
            continue
        expected = EXPECTED_FUTURES[category]
        assert sym["point_value"] == expected["point_value"], (
            f"{code}: point_value={sym['point_value']}, expected={expected['point_value']}"
        )
