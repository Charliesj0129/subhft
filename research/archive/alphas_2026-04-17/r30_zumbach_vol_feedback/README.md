# r30_zumbach_vol_feedback

## Hypothesis
- Zumbach effect: past return trends predict future volatility quadratically (TRA). Down-trends predict excess vol via leverage effect, enabling conditional mean-reversion on TMFD6.

## Formula
- `Z(t) = sum_{i<j} r_i * r_j (quadratic trend); signal = asymmetry(Z_down vs Z_up)`

## Data Fields
- `price`
- `volume`

## Metadata
- `alpha_id`: `r30_zumbach_vol_feedback`
- `paper_refs`: arXiv:1907.06151, arXiv:1609.05177, arXiv:2508.16566
- `complexity`: `O(1)`
