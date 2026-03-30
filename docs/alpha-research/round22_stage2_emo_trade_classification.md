# R22 Stage 2: EMO Trade Classification — Infrastructure

**Date**: 2026-03-28
**Direction**: T1.1 EMO Trade Classification (Ellis, Michaely, O'Hara 2000)
**Status**: Stage 2 COMPLETE — dual APPROVE
**Scope**: Infrastructure (not alpha) — unlocks signed OFI, Hawkes, toxic flow

---

## Algorithm

EMO variant adapted for large-tick TAIFEX futures:
1. `price >= best_ask` → BUY (+1), confidence 1000
2. `price <= best_bid` → SELL (-1), confidence 1000
3. Inside spread: `trade_price*2 vs (best_bid + best_ask)` → BUY/SELL, confidence 800
4. At midpoint: tick rule fallback, confidence 500
5. Crossed market (`bid > ask`): UNKNOWN (0), confidence 0
6. No quotes: UNKNOWN (0), confidence 0

All arithmetic is scaled int (x10000). Zero float. O(1) per tick.

---

## Implementation

| File | Change | LOC |
|------|--------|-----|
| `src/hft_platform/trade_classifier.py` | New: TradeClassifier + _SymbolState | ~140 |
| `src/hft_platform/events.py` | `trade_direction: int = 0`, `trade_confidence: int = 0` on TickEvent | +2 |
| `src/hft_platform/feed_adapter/normalizer.py` | Wire classify() + update_quotes() in all paths | +15 |
| `src/hft_platform/recorder/mapper.py` | Persist `trade_direction` to ClickHouse | +1 |
| `src/hft_platform/migrations/clickhouse/20260328_001_add_trade_direction.sql` | `trade_direction Int8 DEFAULT 0` | 1 |
| `tests/unit/test_trade_classifier.py` | 25 tests | ~250 |

**Total**: ~170 LOC production, ~250 LOC tests

---

## Review Verdicts

### Challenger: APPROVE (after fix round)

| Challenge | Severity | Resolution |
|-----------|----------|------------|
| C1: No kill-gate metrics (confidence discarded) | BLOCKING | FIXED — counters + get_stats() + trade_confidence field |
| C2: Crossed market misclassification | BLOCKING | FIXED — guard returns UNKNOWN when bid > ask |
| C3: Quote staleness risk | Non-blocking | Documented — no timestamp on cached quotes |
| C4: Large-tick IC overpromise (<1.5x not 2-3x) | Non-blocking | Documented — kill gate adjusted expectation |

### Execution: APPROVE (after fix round)

| Check | Result |
|-------|--------|
| E1: Hot-path safety | PASS |
| E2: Backward compatibility | PASS |
| E3: Normalizer integration | PASS |
| E4: Config gating | PASS |
| E5: State management | PASS |
| E6: FeatureEngine impact | PASS |
| E7: ClickHouse persistence | PASS (after fix) |
| E8: Rust boundary | PASS (documented gap) |

**Config drift**: 0

---

## Known Limitations

1. **Fused Rust paths** (`HFT_FUSED_NORMALIZER=1`) skip classification — direction=0
2. **Tuple mode** (`HFT_EVENT_MODE=tuple`) skips classification
3. **Quote staleness**: No timestamp on cached bid/ask
4. **Large-tick accuracy**: Expected IC improvement <1.5x (not 2-3x from equity literature)
5. **`trade_confidence` not persisted**: Only `trade_direction` stored in ClickHouse

---

## Kill Criteria

| Criterion | Measurable? | Method |
|-----------|-------------|--------|
| Signed OFI IC < 1.3x unsigned OFI IC at 30s | YES | Historical backtest |
| >30% tick-rule fallback rate | YES | `classifier.get_stats()` |

---

## Next Steps

- **Stage 2b**: Historical validation on TMFD6/TXFD6 ClickHouse data
- **Downstream unlocks**: Signed OFI, Hawkes branching ratio, VPIN, toxic flow
