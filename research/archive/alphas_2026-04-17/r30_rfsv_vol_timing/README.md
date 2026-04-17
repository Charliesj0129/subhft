# r30_rfsv_vol_timing

## Hypothesis
- Log-realized-vol follows fBm with H~0.1 (rough vol). RFSV forecast outperforms HAR/GARCH, enabling vol-timing position sizing on TMFD6.

## Formula
- `sigma_hat(t+dt) = exp(E[log(sigma)|past] + H*kernel), H estimated via variogram`

## Data Fields
- `price`
- `volume`

## Metadata
- `alpha_id`: `r30_rfsv_vol_timing`
- `paper_refs`: arXiv:1410.3394, arXiv:2312.01426, arXiv:2504.15985
- `complexity`: `O(1)`
