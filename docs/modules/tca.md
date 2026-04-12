# tca — Transaction Cost Analysis

> **Package**: `src/hft_platform/tca/`
> **Files**: 5

## Overview

Transaction Cost Analysis: decision-vs-fill slippage decomposition, maker/taker fee calculation, and reporting.

## Files

| File | Key Exports | Purpose |
|------|-------------|---------|
| `slippage.py` | `SlippageDecomposer`, `SlippageRecord` | Slippage computation |
| `fees.py` | `FeeCalculator` | TAIFEX/TWSE fee calculation |
| + 3 more | — | Reporting, aggregation |

## Slippage Decomposition

```
Decision Price → Arrival Price → Fill Price
     │                 │              │
     └─ Delay Cost ────┘              │
                       └─ Market Impact┘
```

- **Decision price**: LOB mid-price at signal time (from `OrderIntent.decision_price`)
- **Arrival price**: Price at order submit (from `OrderCommand.arrival_price`)
- **Fill price**: Actual execution price (from `FillEvent.price`)

## Fee Structure

Per TAIFEX/TWSE rules:
- Retail, no rebates
- 2.0 bps sell tax on equities
- See `feedback_taifex_fee_structure.md` for details
