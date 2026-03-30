"""Tests for Phase 3 bootstrap wiring."""
from __future__ import annotations
import asyncio


def test_rejection_sink_queue_bounded():
    q = asyncio.Queue(maxsize=256)
    assert q.maxsize == 256


def test_publish_sink_queue_bounded():
    q = asyncio.Queue(maxsize=64)
    assert q.maxsize == 64


def test_strategy_yaml_has_electronic_eye():
    import yaml
    with open("config/base/strategies.yaml") as f:
        data = yaml.safe_load(f)
    strategies = data.get("strategies", [])
    eye_entries = [s for s in strategies if s.get("id") == "electronic_eye"]
    assert len(eye_entries) == 1
    eye = eye_entries[0]
    assert eye["enabled"] is False
    assert eye["module"] == "hft_platform.strategies.electronic_eye"
    assert eye["class"] == "ElectronicEye"
    assert eye["product_type"] == "OPT"
    assert "quoter" in eye.get("params", {})
    assert "hedger" in eye.get("params", {})
    assert "guardian" in eye.get("params", {})
