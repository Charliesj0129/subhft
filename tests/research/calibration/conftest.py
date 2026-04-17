import pandas as pd
import pytest


@pytest.fixture
def sample_ck_export_parquet(tmp_path):
    """Create a minimal CK export parquet file for testing."""
    df = pd.DataFrame({
        "ts_exchange": [1_700_000_000_000_000_000 + i * 10_000_000 for i in range(10)],
        "ts_local": [1_700_000_000_001_000_000 + i * 10_000_000 for i in range(10)],
        "symbol": ["TMFD6"] * 10,
        "side": ["Buy", "Sell"] * 5,
        "price_scaled": [17000_000_000 + i * 1_000_000 for i in range(10)],
        "qty": [1] * 10,
        "fee_scaled": [0] * 10,
    })
    path = tmp_path / "TMFD6_2026-01-27.parquet"
    df.to_parquet(path)
    return path
