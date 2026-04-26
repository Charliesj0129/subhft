"""Shared Shioaji quote-subscription limits.

The cap is in **codes**, but each subscribed code triggers 2 broker topics
(``QuoteType.Tick`` + ``QuoteType.BidAsk`` — see
``subscription_manager._subscribe_symbol``). Solace per-session topic
budget is ~250–256 in practice for SinoPac retail accounts (empirically
measured 2026-04-26: conn 0 with 163 codes → 326 topics → broker accepted
~127 codes (254 topics) and rejected the rest with "Max Num Subscriptions
Exceeded" on session ``(c1,s1)_sinopac``). 120 codes × 2 = 240 topics
keeps a small headroom under that ceiling.

Raise this only if you are also raising it on the broker side and have
re-measured the Solace cap; otherwise enforce the universe limit at the
sharding layer (HFT_QUOTE_CONNECTIONS × cap) instead.
"""

DEFAULT_MAX_SUBSCRIPTIONS_PER_CONN = 120
