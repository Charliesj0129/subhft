import pandas as pd
import pytest


@pytest.fixture
def sample_ck_export_parquet(tmp_path):
    """Create a minimal CK export parquet in instrument-subdirectory layout."""
    inst_dir = tmp_path / "TMFD6"
    inst_dir.mkdir()
    df = pd.DataFrame({
        "ts_exchange": [1_700_000_000_000_000_000 + i * 10_000_000 for i in range(10)],
        "ts_local": [1_700_000_000_001_000_000 + i * 10_000_000 for i in range(10)],
        "symbol": ["TMFD6"] * 10,
        "side": ["Buy", "Sell"] * 5,
        "price_scaled": [17000_000_000 + i * 1_000_000 for i in range(10)],
        "qty": [1] * 10,
        "fee_scaled": [0] * 10,
    })
    path = inst_dir / "2026-01-27.parquet"
    df.to_parquet(path)
    # Return the ck_export root dir (tmp_path), not the file itself
    return tmp_path
