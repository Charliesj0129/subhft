# multilevel_ofi — Multi-Level Order-Flow Imbalance (ref 124)

## Signal

Weighted sum of per-level OFI across L1-L5 depth, smoothed via EMA_8.

```
signal = EMA_8(Σ_{k=1}^{5} w_k · (ΔBid_k - ΔAsk_k))
w_k = exp(-0.5·(k-1)) → [1.0, 0.607, 0.368, 0.223, 0.135]
```

## Status

DRAFT

## Data Fields

`bids`, `asks` — np.ndarray shape (N, 2) with (price, qty) pairs per level.

## Complexity

O(1) — fixed 5 depth levels.
