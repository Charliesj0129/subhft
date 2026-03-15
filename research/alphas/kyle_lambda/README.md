# Kyle's Lambda Alpha

Paper 135: Kyle (1985) "Continuous Auctions and Insider Trading"

Rolling Kyle's Lambda — price impact coefficient estimated via EMA-based
covariance of mid-price changes and signed order flow.

## Formula

```
signed_vol = volume * sign(bid_qty - ask_qty)
lambda     = Cov(dP, signed_vol) / Var(signed_vol)
signal     = clip(lambda / max(EMA_64(|lambda|), eps), -2, 2)
```

## Status

DRAFT — Gate A/B pending.
