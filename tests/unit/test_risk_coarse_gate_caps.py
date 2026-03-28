"""Tests for FastGate + RustValidator using max(all configured caps) (S2)."""

from __future__ import annotations

from unittest.mock import MagicMock

import yaml

from hft_platform.risk.engine import RiskEngine


def _make_config(global_defaults: dict | None = None) -> dict:
    base = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            **(global_defaults or {}),
        },
        "strategies": {},
    }
    return base


def _write_config(tmp_path, config: dict) -> str:
    path = tmp_path / "strategy_limits.yaml"
    path.write_text(yaml.dump(config))
    return str(path)


class TestFastGateMaxCap:
    def test_fast_gate_uses_max_of_all_caps(self, tmp_path, monkeypatch):
        """FastGate should use max(5000, 50000, 10000) = 50000."""
        monkeypatch.setenv("HFT_RISK_FAST_GATE", "1")
        config = _make_config({
            "max_price_cap_futures": 50000.0,
            "max_price_cap_options": 10000.0,
        })
        config_path = _write_config(tmp_path, config)
        q1 = MagicMock()
        q2 = MagicMock()
        engine = RiskEngine(config_path, q1, q2)
        gate = engine._fast_gate
        assert gate is not None
        # max_price should be 50000 * 10000 = 500_000_000
        expected_cap = int(50000.0 * 10000)
        assert gate.max_price_scaled == expected_cap

    def test_fast_gate_without_product_caps_uses_global(self, tmp_path, monkeypatch):
        """FastGate with no product caps uses global 5000."""
        monkeypatch.setenv("HFT_RISK_FAST_GATE", "1")
        config = _make_config()
        config_path = _write_config(tmp_path, config)
        q1 = MagicMock()
        q2 = MagicMock()
        engine = RiskEngine(config_path, q1, q2)
        gate = engine._fast_gate
        assert gate is not None
        expected_cap = int(5000.0 * 10000)
        assert gate.max_price_scaled == expected_cap
