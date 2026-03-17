# cross_ofi_leader

**Cross-Asset OFI from Sector Leader**

## Paper Reference

- **2112.13213** — "Cross-Impact of Order Flow Imbalance in Equity Markets"
- Cont, Cucuringu, Zhang (2021)

## Signal

```
self_OFI_t  = EMA_32( (Δbid_qty - Δask_qty) / (|Δbid_qty| + |Δask_qty| + 1) )
leader_OFI_t = same formula on leader symbol's L1 data
COFI_t = 0.5 * self_OFI_t + 0.5 * leader_OFI_t
```

## Hypothesis

The most liquid sector leader (e.g. TSMC 2330) incorporates market-wide
information first. Its OFI predicts follower stock returns with a short lag.
Combining leader OFI with self OFI improves IC by 40%+ vs self-only.

## Data Fields

`bid_qty`, `ask_qty` (L1) + optional `leader_ofi` from another symbol

## Complexity

O(1) per tick — two scalar EMAs with 8 state variables (`__slots__`).
