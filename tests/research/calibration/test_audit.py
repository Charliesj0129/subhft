import pytest
from research.calibration.audit import InstrumentAuditResult


def test_instrument_audit_result_is_frozen():
    r = InstrumentAuditResult(
        instrument="TMFD6", source="ck_export",
        date_range=("2026-01-27", "2026-02-25"),
        n_trading_days=7, n_fills=150,
        n_fills_with_queue_position=0,
        n_fills_with_decision_price=0,
        n_fills_with_latency=0,
        fill_rate_per_day=21.4,
        instruments_found=["TMFD6"],
        quality_flags=["missing_queue_pos"],
    )
    with pytest.raises((AttributeError, TypeError)):
        r.n_fills = 999


def test_instrument_audit_result_to_dict():
    r = InstrumentAuditResult(
        instrument="TMFD6", source="ck_export",
        date_range=("2026-01-27", "2026-02-25"),
        n_trading_days=7, n_fills=150,
        n_fills_with_queue_position=0,
        n_fills_with_decision_price=0,
        n_fills_with_latency=0,
        fill_rate_per_day=21.4,
        instruments_found=["TMFD6"],
        quality_flags=["missing_queue_pos"],
    )
    d = r.to_dict()
    assert d["instrument"] == "TMFD6"
    assert d["n_fills"] == 150
    assert isinstance(d["quality_flags"], list)


from pathlib import Path

from research.calibration.audit import audit_ck_export_parquet


def test_audit_ck_export_parquet_returns_results(sample_ck_export_parquet):
    results = audit_ck_export_parquet(sample_ck_export_parquet.parent)
    assert len(results) == 1
    r = results[0]
    assert r.instrument == "TMFD6"
    assert r.source == "ck_export"
    assert r.n_fills == 10
    assert r.n_trading_days == 1
    assert "missing_queue_pos" in r.quality_flags


def test_audit_ck_export_parquet_empty_dir_returns_empty(tmp_path):
    results = audit_ck_export_parquet(tmp_path)
    assert results == []
