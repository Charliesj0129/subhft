# shap_microstructure

## Hypothesis
SHAP feature importance reveals that imbalance, spread dynamics, and volume surprise are the three most explanatory LOB features for short-term price prediction. A weighted composite captures their joint signal.

## Formula
signal = EMA_8(0.45 * imbalance + 0.30 * spread_change + 0.25 * vol_surprise)

Where:
- imbalance = (bid_qty - ask_qty) / (bid_qty + ask_qty)
- spread_change = delta of (ask_qty - bid_qty) / (bid_qty + ask_qty)
- vol_surprise = (total - prev_total) / prev_total

## Metadata
- `alpha_id`: `shap_microstructure`
- `paper_refs`: 082
- `complexity`: `O(1)`
- `tier`: ENSEMBLE
- `data_fields`: bid_qty, ask_qty
