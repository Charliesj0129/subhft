# Phase 1 Infrastructure: INF-1 Trade Classification + INF-3 Multi-Window Aggregation

**Date**: 2026-03-28
**Status**: Approved
**Origin**: R22 Master Survey — T1.1 (EMO Trade Classification) + T0.3 (HAR Multi-Window)

---

## Goal

Two infrastructure pieces that unlock downstream alpha research:
1. **INF-1**: EMO trade classification — label each tick as buyer/seller-initiated
2. **INF-3**: Multi-window EMA aggregation — extend tick-level features to tradeable horizons

## Scope

### INF-1: Trade Classification (DONE — awaiting commit)

EMO algorithm adapted for large-tick TAIFEX futures. Already implemented and dual-APPROVE reviewed.

**Files**:
- `src/hft_platform/trade_classifier.py` — TradeClassifier + _SymbolState (~140 LOC)
- `src/hft_platform/events.py` — `trade_direction: int = 0`, `trade_confidence: int = 0` on TickEvent
- `src/hft_platform/feed_adapter/normalizer.py` — classify() + update_quotes() wired in TickEvent mode; tuple-mode fast paths do not propagate trade classification (see normalizer.py:557, :591)
- `src/hft_platform/recorder/mapper.py` — persist trade_direction to ClickHouse
- `src/hft_platform/migrations/clickhouse/20260328_001_add_trade_direction.sql`
- `tests/unit/test_trade_classifier.py` — 25 tests

**Algorithm**:
1. price >= best_ask -> BUY (+1), confidence 1000
2. price <= best_bid -> SELL (-1), confidence 1000
3. Inside spread: trade_price*2 vs (best_bid + best_ask), confidence 800
4. At midpoint: tick rule fallback, confidence 500
5. Crossed market (bid > ask): UNKNOWN (0), confidence 0
6. No quotes: UNKNOWN (0), confidence 0

All arithmetic is scaled int (x10000). Zero float. O(1) per tick.

**Kill criteria**: signed OFI IC < 1.3x unsigned at 30s -> KILL; >30% tick-rule fallback -> KILL.

**ClickHouse migration**: `trade_direction Int8 DEFAULT 0` column addition. Migration file included; operational deployment status is outside this spec's scope.

### INF-3: Multi-Window EMA Aggregation (TO IMPLEMENT)

Embed 5 multi-window EMA features into FeatureEngine as `lob_shared_v3`.

Current `lob_shared_v2` has 22 features indexed [0]-[21] (verified at `registry.py:140-193` and `test_feature_engine_v2.py:85`). New features extend to [22]-[26], making v3 = 27 features total.

**Architecture decision**: FeatureEngine inline (Option A), not separate AggregationEngine module.

**Rationale**:
- Minimal viable = only 5 features, tuple growth 22->27 is acceptable
- Strategy consumption unchanged (FeatureUpdateEvent.values[idx])
- One fewer module = one fewer failure point
- Python-first implementation; Rust kernel parity deferred (current Rust kernel only covers `lob_shared_v1`, see `test_feature_schema_parity.py:269`)

#### New Features

| Index | Feature ID | Input | Window | Alpha | Purpose |
|-------|-----------|-------|--------|-------|---------|
| [22] | `ofi_l1_ema5s` | ofi_l1_raw [0] | 5s (40 ticks) | 2/41 | Ultra-short flow pressure |
| [23] | `ofi_l1_ema30s` | ofi_l1_raw [0] | 30s (240 ticks) | 2/241 | Medium flow (CBS signal window) |
| [24] | `imbalance_ema5s_ppm` | l1_imbalance_ppm [10] | 5s (40 ticks) | 2/41 | Ultra-short directional |
| [25] | `spread_ema30s` | spread_scaled [3] | 30s (240 ticks) | 2/241 | Spread regime (medium) |
| [26] | `spread_ema300s` | spread_scaled [3] | 300s (2400 ticks) | 2/2401 | Spread regime (long-term) |

Alpha constants based on 125ms median tick cadence (TXFD6).

#### What is NOT aggregated and why

| Feature | 300s EMA | Reason |
|---------|----------|--------|
| OFI | NO | R19: OFI OU tau=15s, e^(-300/15) ~ 0. Signal is noise at 300s. |
| VRR | NO | Already a 5s/300s ratio. Aggregating a ratio is meaningless. |
| Imbalance | 5s only | Snapshot feature, half-life < 1s. 30s+ is noise. |
| Signed OFI | NO | Phase 2 — pending INF-1 IC validation (kill gate 1.3x). |

#### Implementation Plan

**`feature/registry.py`**:
- Add `lob_shared_v3` FeatureSet with 27 features (v2's 22 + 5 new)
- v2 remains registered for backward compatibility
- v3 becomes default

**Default feature-set compatibility** (required updates when v3 becomes default):
- `registry.py:102` — default feature-set constant must point to `lob_shared_v3`
- `alpha/_gate_d.py:232` — feature-set check in promotion gate must accept v3
- `test_strategy_feature_compat.py:7` — compatibility assertion must include v3
- `strategies/opportunistic_mm.py:36` — feature-set reference (verify no hardcoded v2 assumption)
- All existing v2 consumers continue to work if they use `FeatureUpdateEvent.get(feature_id)` (name-based access). Consumers using positional `values[idx]` for indices [0]-[21] are unaffected since v3 is a superset.

**`feature/engine.py`**:
- `_LobKernelState`: add 5 float fields (`agg_ofi_ema5s`, `agg_ofi_ema30s`, `agg_imb_ema5s`, `agg_spread_ema30s`, `agg_spread_ema300s`)
- `FeatureEngine.__init__`: add 3 alpha constants to `__slots__` (`_alpha_5s`, `_alpha_30s`, `_alpha_300s`)
- `_compute_values()`: append 5 EMA updates at tail, output as scaled int

**EMA update** (O(1), zero allocation):
```python
ks.agg_ofi_ema5s += self._alpha_5s * (ofi_l1_raw - ks.agg_ofi_ema5s)
# output: int(ks.agg_ofi_ema5s)  # same scale as input
```

**Warmup**:
- 5s features: warmup_min_events = 40
- 30s features: warmup_min_events = 240
- 300s features: warmup_min_events = 2400 (same as VRR)
- Max warmup across all features remains 2400

#### Tests

- Feature count assertion: 22 -> 27
- EMA convergence: feed constant input, verify EMA converges to that value
- Alpha correctness: verify 2/(N+1) formula
- Warmup mask: features [22]-[26] report not-ready before respective warmup counts
- No regression on features [0]-[21]
- v3 default compatibility: verify all consumers listed above work with v3

## What This Unlocks (Phase 2+)

| Direction | Dependency | Status |
|-----------|-----------|--------|
| Signed OFI feature | INF-1 validated (IC > 1.3x) | Phase 2 |
| Hawkes branching ratio | INF-1 validated | Phase 2 |
| CBS execution optimizer | INF-3 spread_ema + imbalance_ema | Phase 2 |
| CBS regime-adaptive params | INF-3 spread_ema300s + VRR | Phase 2 |
| VPIN (tick-rule based) | INF-1 validated | Phase 2 |

## Known Limitations

1. Fused Rust paths (`HFT_FUSED_NORMALIZER=1`) skip trade classification
2. INF-1 quote staleness: no timestamp on cached bid/ask
3. Large-tick IC improvement expected < 1.5x (not 2-3x from equity literature)
4. trade_confidence not persisted to ClickHouse (real-time only)
5. INF-3 alpha constants assume 125ms tick cadence — will drift on instruments with different tick rates
6. INF-3 is Python-only; Rust kernel parity covers `lob_shared_v1` only — v3 features run Python path regardless of `HFT_FEATURE_ENGINE_BACKEND` setting
7. INF-1 tuple-mode normalizer fast paths do not propagate trade classification; classification only active in TickEvent mode
