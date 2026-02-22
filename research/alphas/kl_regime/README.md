# kl_regime

## Hypothesis
- Return distribution drift signals latent market regime transition.
- KL divergence between recent and reference windows provides a robust shift flag.

## Formula
- `T = 2 * (n*m/(n+m)) * D_KL(P_recent || P_ref)`
- `is_shift = p_value(T, chi2_df=n_bins-1) < threshold_p`

## Metadata
- `alpha_id`: `kl_regime`
- `paper_refs`: 009
- `complexity`: `O(N)`
