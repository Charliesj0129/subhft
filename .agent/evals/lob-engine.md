# Eval: LOB Engine

**Component**: `src/hft_platform/feed_adapter/lob_engine.py`

## Capability

### C1: Book Update
- Accepts normalized quotes and updates the internal limit order book.
- Maintains sorted price levels: bids descending, asks ascending.
- Handles insert, update, and delete of price levels.

### C2: Snapshot Reconstruction
- Can reconstruct full book state from a snapshot message.
- Snapshot replaces all existing levels (not incremental).

### C3: Best Bid/Ask
- Returns current best bid and best ask in O(1).
- Returns `None` for empty sides (not zero, not crash).

### C4: Depth Query
- Returns top-N levels for bid and ask sides.
- If fewer than N levels exist, returns available levels without padding.

### C5: Crossed Book Detection
- Detects and handles crossed books (best bid >= best ask).
- Logs warning and does not propagate invalid state downstream.

## Regression

### R1: Latency
- **Update**: < 100us mean per single-level update.
- **Snapshot**: < 500us for full 5-level book reconstruction.
- Benchmark: `tests/benchmark/micro_bench_lob.py`

### R2: Memory
- Book state uses contiguous numpy arrays or pre-allocated buffers.
- No dict-of-dicts or nested object structures for price levels.

### R3: Correctness
- After N random updates, book must remain sorted.
- Bid/ask spread must be non-negative (or flagged as crossed).
