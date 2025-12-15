"""
Data Loader for Research and Backtesting

Provides unified access to historical market data from WAL files and ClickHouse.
"""

import os
import json
import glob
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False

from structlog import get_logger

logger = get_logger("research.data_loader")


class DataLoader:
    """
    Unified data loader for research notebooks and backtesting.
    
    Supports loading from:
    - WAL files (local JSON-lines)
    - ClickHouse (if configured)
    - Sample data files
    """
    
    def __init__(
        self,
        wal_dir: str = ".wal",
        data_dir: str = "data",
        clickhouse_host: Optional[str] = None
    ):
        self.wal_dir = wal_dir
        self.data_dir = data_dir
        self.clickhouse_host = clickhouse_host or os.getenv("CLICKHOUSE_HOST")
        self._ch_client = None
        
    def _get_ch_client(self):
        """Lazy-load ClickHouse client."""
        if self._ch_client is None and self.clickhouse_host:
            try:
                import clickhouse_connect
                self._ch_client = clickhouse_connect.get_client(
                    host=self.clickhouse_host,
                    port=8123,
                    username="default",
                    password=""
                )
            except Exception as e:
                logger.warning("ClickHouse not available", error=str(e))
        return self._ch_client
    
    def load_market_data(
        self,
        symbol: str,
        start: str,
        end: str,
        source: str = "auto"
    ) -> "pd.DataFrame":
        """
        Load market data for a symbol within a date range.
        
        Args:
            symbol: Stock symbol (e.g., "2330")
            start: Start date (YYYY-MM-DD)
            end: End date (YYYY-MM-DD)
            source: "wal", "clickhouse", or "auto"
            
        Returns:
            DataFrame with columns: timestamp, price, volume, bid, ask
        """
        if not HAS_PANDAS:
            raise ImportError("pandas is required for DataLoader. Install with: pip install pandas")
        
        if source == "auto":
            # Try ClickHouse first, fallback to WAL
            if self._get_ch_client():
                return self._load_from_clickhouse(symbol, start, end)
            return self._load_from_wal(symbol, start, end)
        elif source == "clickhouse":
            return self._load_from_clickhouse(symbol, start, end)
        else:
            return self._load_from_wal(symbol, start, end)
    
    def _load_from_wal(self, symbol: str, start: str, end: str) -> "pd.DataFrame":
        """Load data from WAL JSON-lines files."""
        import pandas as pd
        
        rows = []
        pattern = os.path.join(self.wal_dir, "market_data_*.jsonl")
        
        for fpath in glob.glob(pattern):
            try:
                with open(fpath, "r") as f:
                    for line in f:
                        try:
                            row = json.loads(line)
                            if row.get("symbol") == symbol:
                                rows.append(row)
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.warning("Failed to read WAL file", path=fpath, error=str(e))
        
        if not rows:
            logger.warning("No data found in WAL", symbol=symbol)
            return pd.DataFrame()
        
        df = pd.DataFrame(rows)
        
        # Convert timestamps if present
        if "exch_ts" in df.columns:
            df["timestamp"] = pd.to_datetime(df["exch_ts"], unit="ns")
            df = df.set_index("timestamp").sort_index()
        
        return df
    
    def _load_from_clickhouse(self, symbol: str, start: str, end: str) -> "pd.DataFrame":
        """Load data from ClickHouse."""
        import pandas as pd
        
        client = self._get_ch_client()
        if not client:
            raise ConnectionError("ClickHouse not available")
        
        query = f"""
            SELECT * FROM hft.market_data
            WHERE symbol = '{symbol}'
              AND toDate(fromUnixTimestamp64Nano(exch_ts)) >= '{start}'
              AND toDate(fromUnixTimestamp64Nano(exch_ts)) <= '{end}'
            ORDER BY exch_ts
        """
        
        try:
            result = client.query(query)
            df = pd.DataFrame(result.result_rows, columns=result.column_names)
            if "exch_ts" in df.columns:
                df["timestamp"] = pd.to_datetime(df["exch_ts"], unit="ns")
                df = df.set_index("timestamp")
            return df
        except Exception as e:
            logger.error("ClickHouse query failed", error=str(e))
            return pd.DataFrame()
    
    def list_symbols(self) -> List[str]:
        """List available symbols from WAL files."""
        symbols = set()
        pattern = os.path.join(self.wal_dir, "market_data_*.jsonl")
        
        for fpath in glob.glob(pattern):
            try:
                with open(fpath, "r") as f:
                    for line in f:
                        try:
                            row = json.loads(line)
                            if "symbol" in row:
                                symbols.add(row["symbol"])
                        except:
                            continue
                        if len(symbols) > 100:  # Limit scan
                            break
            except:
                continue
        
        return sorted(symbols)
    
    def load_sample(self, name: str = "sample_feed") -> "pd.DataFrame":
        """Load sample data for testing."""
        import pandas as pd
        
        # Try NPZ format
        npz_path = os.path.join(self.data_dir, f"{name}.npz")
        if os.path.exists(npz_path):
            import numpy as np
            data = np.load(npz_path, allow_pickle=True)
            return pd.DataFrame(data["arr_0"])
        
        # Try CSV
        csv_path = os.path.join(self.data_dir, f"{name}.csv")
        if os.path.exists(csv_path):
            return pd.read_csv(csv_path)
        
        logger.warning("Sample data not found", name=name)
        return pd.DataFrame()
