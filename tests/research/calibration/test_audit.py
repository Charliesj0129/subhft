import pytest

from research.calibration.audit import InstrumentAuditResult, audit_ck_export_parquet


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


def test_audit_ck_export_parquet_skips_invalid_date_filename(tmp_path):
    """Files like 'TMFD6_bad_extra.parquet' (non-ISO date) should be skipped."""
    import pandas as pd
    bad = tmp_path / "TMFD6_bad_extra.parquet"
    pd.DataFrame({"a": [1]}).to_parquet(bad)
    good = tmp_path / "TMFD6_2026-03-19.parquet"
    pd.DataFrame({"a": [1]}).to_parquet(good)
    results = audit_ck_export_parquet(tmp_path)
    # Only the good file should be counted
    assert len(results) == 1
    assert results[0].n_trading_days == 1
    assert results[0].date_range == ("2026-03-19", "2026-03-19")


def test_audit_ck_export_parquet_accepts_partial_suffix(tmp_path):
    """Files like 'TXFC6_2026-03-17_partial.parquet' should be accepted."""
    import pandas as pd
    f = tmp_path / "TXFC6_2026-03-17_partial.parquet"
    pd.DataFrame({"a": [1, 2, 3]}).to_parquet(f)
    results = audit_ck_export_parquet(tmp_path)
    assert len(results) == 1
    assert results[0].instrument == "TXFC6"
    assert results[0].date_range == ("2026-03-17", "2026-03-17")
