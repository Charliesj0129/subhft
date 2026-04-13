# tca — Transaction Cost Analysis

> **Package**: `src/hft_platform/tca/`
> **Files**: 6
> **Runtime Plane**: Cold (offline analysis + live fee computation)

## Overview

Transaction Cost Analysis module for Taiwan futures. Two execution contexts:

1. **Live path** (`FeeCalculator`): Pure integer arithmetic — called per fill in `ExecutionRouter`. Precision Law compliant.
2. **Offline path** (`TCAAnalyzer`, `SlippageDecomposer`, `TCAReportGenerator`): Float arithmetic — queries ClickHouse `hft.fills` for daily cost reporting and slippage decomposition.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `types.py` | `FeeBreakdown`, `SlippageBreakdown`, `TCADailyReport` | Frozen dataclasses (`__slots__`); `FeeBreakdown` is scaled x10000 int, `SlippageBreakdown` is float bps |
| `fee_calculator.py` | `FeeCalculator` | Pure-int per-fill fee computation. Loads YAML fee schedules. Handles commission + tax (per-contract or percentage-based) |
| `slippage.py` | `SlippageDecomposer` | 4-component slippage breakdown (float, offline only) |
| `analyzer.py` | `TCAAnalyzer` | Queries ClickHouse `hft.fills`, produces `TCADailyReport` per (strategy, symbol) key |
| `report.py` | `TCAReportGenerator` | Formats `TCADailyReport` into Telegram HTML sections |
| `__init__.py` | — | Re-exports: `TCAAnalyzer`, `FeeCalculator`, `FeeBreakdown`, `SlippageBreakdown`, `TCADailyReport` |

## Slippage Decomposition (Three-Price Model)

```
Decision Price → Arrival Price → Fill Price
     │                 │              │
     └─ Delay Cost ────┘              │
                       └─ Exec Cost ──┘
```

- **Decision price**: LOB mid-price at signal time (`OrderIntent.decision_price`, scaled x10000)
- **Arrival price**: Price at order submit (`OrderCommand.arrival_price`, stamped by `OrderAdapter` via `mid_price_fn`)
- **Fill price**: Actual execution price (`FillEvent.price`, scaled x10000)

Cost components (all reported in bps):

| Component | Formula | Source |
|-----------|---------|--------|
| Commission | `fee_ntd / notional_ntd × 10000` | `FeeCalculator.compute()` |
| Tax | `tax_ntd / notional_ntd × 10000` | YAML schedule, side-dependent |
| Delay cost | `(arrival - decision) × point_value / notional × 10000` | Timestamp gap |
| Execution cost | `(fill - arrival) × point_value / notional × 10000` | Market impact |

## Fee Calculation (Live Path)

`FeeCalculator.compute(symbol, side, qty, price_scaled) → FeeBreakdown`

- All arithmetic is **pure integer** — no float, no Decimal
- Fee schedules loaded from `config/base/fees/futures.yaml`
- Symbol → product resolution via `symbol_map` in YAML (e.g. `TXFD6 → TX`)
- Tax applied conditionally by `tax_side` (`sell`, `buy`, or `both`)
- Two tax modes: flat `tax_per_contract` or percentage `tax_rate_bps`

## Integration Points

| Caller | How | When |
|--------|-----|------|
| `ExecutionRouter` | Enriches `FillEvent` with `decision_price` + `arrival_price` from cached `OrderCommand` | Per fill |
| `OrderAdapter` | Stamps `arrival_price` via `mid_price_fn` callback | Per order submit |
| `DailyReportService` | Calls `TCAAnalyzer.daily_report()` + `TCAReportGenerator.format_telegram_section()` | End of day |
| CLI `hft tca` | `cmd_tca_daily()` → tabular console output | On demand |

## CLI

```bash
uv run hft tca --date 2026-04-12
```

Queries ClickHouse `hft.fills`, prints per-(strategy, symbol) table with trade count, volume, commission/tax/total in bps.

## ClickHouse Schema

Migration `20260327_002_add_tca_columns_to_fills.sql` adds to `hft.fills`:

| Column | Type | Default | Purpose |
|--------|------|---------|---------|
| `decision_price` | `Int64` | `0` | LOB mid at signal time (x10000) |
| `arrival_price` | `Int64` | `0` | Price at order submit (x10000) |

## Configuration

Fee schedule: `config/base/fees/futures.yaml`

```yaml
futures:
  TX:
    commission_per_contract: 20
    tax_rate_bps: 2.0
    tax_side: sell
    tick_size: 1
    point_value: 200
  MTX:
    commission_per_contract: 10
    ...
symbol_map:
  TXFD6: TX
  MXFD6: MTX
```
