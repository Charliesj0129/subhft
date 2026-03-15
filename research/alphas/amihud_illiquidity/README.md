# amihud_illiquidity

## Hypothesis
- Amihud (2002) illiquidity ratio |return|/volume measures price impact per unit volume. High illiquidity indicates information-driven trading and low liquidity.

## Formula
- `AI_t = EMA_16(|ΔP/P| / max(volume, ε))`

## Metadata
- `alpha_id`: `amihud_illiquidity`
- `paper_refs`: Amihud (2002)
- `complexity`: `O(1)`
- `data_fields`: `("mid_price", "volume")`
