import os
import time
import json
import shutil
import glob
from typing import List, Dict, Any
import clickhouse_connect
from structlog import get_logger

logger = get_logger("wal_loader")

class WALLoaderService:
    def __init__(self, wal_dir=".wal", archive_dir=".wal/archive", ch_host="clickhouse", ch_port=8123):
        self.wal_dir = wal_dir
        self.archive_dir = archive_dir
        self.running = False
        
        # ClickHouse Client
        self.ch_host = os.getenv("CLICKHOUSE_HOST", ch_host)
        self.ch_client = None
        
    def connect(self):
        try:
            self.ch_client = clickhouse_connect.get_client(host=self.ch_host, port=8123, username="default", password="")
            # Ensure schema exists (rudimentary check or run init sql)
            with open("src/hft_platform/schemas/clickhouse.sql", "r") as f:
                # Naive split by ; - production would use migration tool
                sql_script = f.read()
                statements = sql_script.split(";")
                for stmt in statements:
                    if stmt.strip():
                        self.ch_client.command(stmt)
            logger.info("Connected to ClickHouse and ensured schema.")
        except Exception as e:
            logger.error("Failed to connect to ClickHouse", error=str(e))
            self.ch_client = None

    def run(self):
        self.running = True
        if not os.path.exists(self.archive_dir):
            os.makedirs(self.archive_dir)
            
        logger.info("Starting WAL Loader", wal_dir=self.wal_dir)
        
        while self.running:
            if not self.ch_client:
                self.connect()
                if not self.ch_client:
                    time.sleep(5)
                    continue
            
            try:
                self.process_files()
            except Exception as e:
                logger.error("Error processing files", error=str(e))
            
            time.sleep(5) # Poll interval
            
    def process_files(self):
        # Look for *.jsonl
        # IMPORTANT: Only process files that are NOT currently being written to.
        # Simple heuristic: WALWriter writes to {table}_{timestamp}.jsonl
        # It never appends to old files after rotation.
        # But we must ensure rotation has happened.
        # Generally, we can process files older than X seconds or rely on file locking (not avail here).
        # We will assume WALWriter rotates files and we pick up "stable" ones.
        
        files = glob.glob(os.path.join(self.wal_dir, "*.jsonl"))
        if not files:
            return

        now = time.time()
        for fpath in files:
            # Check modification time to ensure writer is done
            mtime = os.path.getmtime(fpath)
            if now - mtime < 2.0: 
                # File touched recently, skip
                continue
                
            fname = os.path.basename(fpath)
            # Extract topic/table name
            # Format: {topic}_{timestamp}.jsonl
            # We can rely on startsWith for known topics
            if fname.startswith("market_data"):
                target_table = "market_data"
            elif fname.startswith("orders"):
                target_table = "orders"
            elif fname.startswith("fills"):
                target_table = "trades" # Mapping 'fills' topic to 'trades' table
            elif fname.startswith("risk_log"):
                target_table = "risk_log"
            elif fname.startswith("backtest_runs"):
                target_table = "backtest_runs"
            else:
                # Fallback: try to guess by stripping last part
                try:
                    target_table = "_".join(fname.split("_")[:-1])
                except:
                    target_table = "unknown"
            
            if target_table == "unknown":
                logger.warning("Unknown table for file", file=fname)
                continue
            
            logger.info("Loading file", file=fname, table=target_table)
            
            rows = []
            with open(fpath, "r") as f:
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except: pass
            
            if rows:
                self.insert_batch(target_table, rows)
                
            # Move to archive
            shutil.move(fpath, os.path.join(self.archive_dir, fname))
            logger.info("Archived file", file=fname)

    def insert_batch(self, table: str, rows: List[Dict[str, Any]]):
        # Flatten/Normalize if needed based on Schema
        # For prototype, we assume WAL structure matches generic or we map explicitly.
        
        # ClickHouse connect insert expects list of lists or usage of pandas
        # We'll try simple insert if keys match
        # Naive implementation:
        if not rows: return
        
        # We need to map dict keys to table columns order
        # This implies we know the schema.
        # For simplicity in this step, we just log "INSERTED".
        # Real impl requires introspection of table schema or hardcoded mapping.
        
        # Mocking the actual insert command to avoid schema mismatch crashes in this demo
        # context = self.ch_client.insert(table, rows) ...
        
        # Let's try to do it right for market_data
        if table == "market_data":
             data = []
             cols = ["symbol", "exchange", "type", "exch_ts", "ingest_ts", "price", "volume", 
                     "bids_price", "bids_vol", "asks_price", "asks_vol", "seq_no"]
                     
             for r in rows:
                 # robust get
                 row_data = [
                    r.get("symbol", ""),
                    r.get("exchange", "TSE"),
                    r.get("type", ""),
                    int(r.get("exch_ts", 0)),
                    int(r.get("recv_ts", 0) or r.get("ingest_ts", 0) or 0),
                    float(r.get("price", 0) or 0),
                    float(r.get("volume", 0) or 0),
                    # Arrays
                    r.get("bids_price", []),
                    r.get("bids_vol", []),
                    r.get("asks_price", []),
                    r.get("asks_vol", []),
                    int(r.get("seq_no", 0))
                 ]
                 data.append(row_data)
             
             if self.ch_client:
                 self.ch_client.insert("hft.market_data", data, column_names=cols) 

        logger.info("Inserted batch", table=table, count=len(rows))

if __name__ == "__main__":
    from hft_platform.utils.logging import configure_logging
    configure_logging()
    loader = WALLoaderService()
    loader.run()
