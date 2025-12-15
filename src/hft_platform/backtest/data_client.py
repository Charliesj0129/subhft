import time
from typing import List, Dict, Any, Generator
from datetime import datetime, timedelta
# clickhouse_connect is assumed to be the driver of choice as per recorder
import clickhouse_connect
from structlog import get_logger

logger = get_logger("ch_streamer")

class ClickHouseStreamer:
    def __init__(self, host="localhost", port=8123, username="default", password="", database="default"):
        self.client = clickhouse_connect.get_client(
            host=host, port=port, username=username, password=password, database=database
        )
        
    def stream_market_data(self, 
                           symbols: List[str], 
                           start_ts: datetime, 
                           end_ts: datetime, 
                           chunk_size: int = 50000) -> Generator[Dict[str, Any], None, None]:
        """
        Stream market data sorted by time.
        In a real scenario with huge data, we'd query by day partitions or use
        keyset pagination (WHERE (ts, symbol) > (last_ts, last_symbol)).
        For simplicity, we use OFFSET/LIMIT or simple time ranging if data fits in RAM partitions.
        Best approach for reliability: Time-based chunking.
        """
        
        current_start = start_ts
        delta = timedelta(minutes=30) # 30 min chunks to safeguard RAM
        
        while current_start < end_ts:
            current_end = min(current_start + delta, end_ts)
            
            logger.info("Fetching chunk", start=current_start, end=current_end)
            
            # Use raw SQL to get dicts or tuples
            # Schema: 
            # symbol, type, price, volume, bids_price, bids_vol ...
            # We assume stored layout matches what we need for Replay.
            # Simplified query:
            query = f"""
                SELECT 
                    symbol, type, 
                    price, volume, 
                    exch_ts, ingest_ts,
                    bids_price, bids_vol,
                    asks_price, asks_vol
                FROM market_data
                WHERE symbol IN {{symbols:Array(String)}}
                  AND exch_ts >= {{start:DateTime64}}
                  AND exch_ts < {{end:DateTime64}}
                ORDER BY exch_ts ASC
            """
            
            params = {
                "symbols": symbols,
                "start": current_start,
                "end": current_end
            }
            
            # Using query_df might be faster but we need row iteration.
            # stream_query is ideal if supported, else chunked fetch.
            # clickhouse_connect query returns a wrapper we can iterate.
            try:
                result = self.client.query(query, parameters=params)
                
                # Column mapping
                cols = result.column_names
                
                for row in result.result_set:
                    row_dict = dict(zip(cols, row))
                    # Basic normalization for replay
                    yield row_dict
                    
            except Exception as e:
                logger.error("Query failed", error=str(e))
                raise
                
            current_start = current_end
            
    def close(self):
        self.client.close()
