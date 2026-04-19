"""Tests for TakerEngine wrapper."""

from research.backtest.taker_engine import TakerEngine


def test_taker_engine_properties():
    engine = TakerEngine()
    assert engine.engine_type == "taker"
    assert "PowerProb" in engine.fill_model_name or engine.fill_model_name != ""
