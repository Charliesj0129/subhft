# feature — LOB Feature Engine

> **Package**: `src/hft_platform/feature/`
> **Runtime Plane**: Feature
> **Hot-Path**: `FeatureEngine.process_lob_stats()`, `BurstDetector.on_tick()`

## Overview

Modular, versioned LOB feature computation engine with Rust/Python dual-kernel support, profile-based configuration, and rollout/A-B testing capabilities.

## Schema Versions

| Version | Features | Default | Key Additions |
|---------|----------|---------|---------------|
| `lob_shared_v1` | 16 | No | Stateless L1 + rolling OFI/EMA |
| `lob_shared_v2` | 22 | No | Depth-norm OFI, autocov, TOB survival, ISS, MLDM, toxicity |
| `lob_shared_v3` | 27 | **Yes** | Multi-window EMA aggregation (5s/30s/300s) |

### v3 Feature Index (27 features)

| Idx | Feature ID | Scale | Warmup | Source |
|-----|-----------|-------|--------|--------|
| 0-1 | best_bid, best_ask | x10000 | 1 | book |
| 2-3 | mid_price_x2, spread_scaled | x10000 | 1 | book |
| 4-5 | bid_depth, ask_depth | raw | 1 | book |
| 6 | depth_imbalance_ppm | x1M | 1 | book |
| 7 | microprice_x2 | x10000 | 1 | book |
| 8-10 | l1_bid_qty, l1_ask_qty, l1_imbalance_ppm | raw/x1M | 1 | book |
| 11-13 | ofi_l1_raw, ofi_l1_cum, ofi_l1_ema8 | raw | 2 | book |
| 14-15 | spread_ema8, depth_imbalance_ema8_ppm | x10000/x1M | 2 | book |
| 16 | ofi_depth_norm_ppm | x1M | 8 | book (v2) |
| 17 | ret_autocov_5s_x1e6 | x1M | 42 | book (v2) |
| 18 | tob_survival_ms | raw | 2 | book (v2) |
| 19 | impact_surprise_x1000 | x1000 | 400 | book (v2) |
| 20 | deep_depth_momentum_x1000 | x1000 | 128 | book (v2) |
| 21 | toxicity_ema50_x1000 | x1000 | 50 | tick (v2) |
| 22-23 | ofi_l1_ema5s, ofi_l1_ema30s | raw | 40/240 | book (v3) |
| 24 | imbalance_ema5s_ppm | x1M | 40 | book (v3) |
| 25-26 | spread_ema30s, spread_ema300s | x10000 | 240/2400 | book (v3) |

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `engine.py` | `FeatureEngine` | Core stateful computation (1076 lines) |
| `registry.py` | `FeatureRegistry`, `FeatureSet`, `FeatureSpec` | Versioned feature-set definitions |
| `kernel.py` | `LobFeatureKernel`, `RustFeatureKernelAdapter` | Python/Rust dual kernel |
| `profile.py` | `FeatureProfile`, `FeatureProfileRegistry` | Parameter profiles (YAML-backed) |
| `rollout.py` | `FeatureRolloutController`, `FeatureRolloutAssignment` | A-B testing and rollout state |
| `boundary.py` | `TypedFeatureFrameV1` | Python/Rust transport serialization |
| `burst_detector.py` | `BurstDetector` | Tick intensity surge detection |
| `compat.py` | `check_feature_profile_compat`, `check_runtime_feature_engine_compat` | Compatibility validation |

## Key APIs

```python
# Bootstrap
engine = FeatureEngine(feature_set_id="lob_shared_v3", kernel_backend="rust")

# Process LOB update → returns FeatureUpdateEvent or None
event = engine.process_lob_stats(lob_stats_event)

# Trade data (for toxicity feature)
engine.on_tick(symbol, price, volume, trade_direction, trade_confidence)

# Feature access
val = engine.get_feature("TXFD6", "toxicity_ema50_x1000")
tup = engine.get_feature_tuple("TXFD6")  # All 27 values as tuple
```

## BurstDetector

```python
detector = BurstDetector(window_ns=30_000_000_000, multiplier=3.0, cooldown_ns=5_000_000_000)
is_burst = detector.on_tick(ts_ns)  # True on rising-edge detection
```

- Pre-allocated circular buffer (Allocator Law compliant)
- EMA baseline NOT updated during burst (prevents contamination)
- Cooldown gate between signals

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `HFT_FEATURE_ENGINE_ENABLED` | `1` | Enable/disable FeatureEngine |
| `HFT_FEATURE_ENGINE_BACKEND` | `python` | Kernel backend: `python` or `rust` |
| `HFT_FEATURE_ENGINE_EMIT_EVENTS` | `1` | Emit FeatureUpdateEvent to bus |
| `HFT_FEATURE_PROFILE_ID` | — | Override active profile |
| `HFT_FEATURE_PROFILES_CONFIG` | `config/feature_profiles.yaml` | Profile registry path |
| `HFT_FEATURE_ROLLOUT_STATE_PATH` | `outputs/feature_rollout_state.json` | Rollout state path |

## Quality Flags

| Flag | Bit | Meaning |
|------|-----|---------|
| GAP | 0 | Bus overflow detected |
| STATE_RESET | 1 | Symbol state was reset |
| STALE_INPUT | 2 | Input event is stale |
| OUT_OF_ORDER | 3 | Sequence regression |
| PARTIAL | 4 | Not all features computed |
