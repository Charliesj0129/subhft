# Eval: Feed Normalizer

**Component**: `src/hft_platform/feed_adapter/normalizer.py`
**Rust Fast Path**: `rust_core/src/normalize.rs`

## Capability

### C1: Quote Normalization
- Converts raw broker quote payload (Shioaji format) to internal `NormalizedQuote`.
- Prices are scaled to integer micros (`price * SCALE`), never stored as float.
- Volumes are preserved as integers.

### C2: One-Sided Book Handling
- If only bid side is present (ask is empty/None), produce a valid quote with empty ask array.
- If only ask side is present (bid is empty/None), produce a valid quote with empty bid array.
- Never crash or raise on missing sides.

### C3: Zero/Negative Price Filtering
- Prices <= 0 are filtered out before normalization.
- Corresponding volumes are also removed.

### C4: Rust/Python Parity
- Rust `normalize_quote` and Python fallback must produce identical output for the same input.
- Test with identical payloads and assert equality.

## Regression

### R1: Latency
- **Python path**: < 50us mean for a 5-level book.
- **Rust path**: < 5us mean for a 5-level book.
- Benchmark: `tests/benchmark/micro_bench_normalizer.py`

### R2: Allocation
- No heap allocation per tick in the Rust path.
- Python path: pre-allocated numpy arrays only, no list comprehensions in hot loop.

### R3: Throughput
- Must sustain > 100,000 normalizations/second (Python).
- Must sustain > 1,000,000 normalizations/second (Rust).
