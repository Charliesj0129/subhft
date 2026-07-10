"""PoC-reversion rule derived from 'Delta Volume Profile Pro v11' (DVP Pro).

The published script (fabricionicolauxx, MPL-2.0, BigBeluga Quadro-Profile core)
is a *display-only* indicator: a rolling volume profile (PoC + buy/sell quads)
plus a Daily VWAP anchor and an 80-symbol proxy-volume engine. It codes no entry,
exit, or directional rule.

The one mechanical hypothesis it *implies* -- and the one the user asked to test
-- is volume-node mean reversion: price that pushes outside the profile's 70%
Value Area tends to revert toward the Point-of-Control (the max-volume node).
We build exactly that, parameter-free (lookBack=200/bins=60 defaults, VA=70%
convention, structural stop = profile extreme), and score it honestly.

NOTE: distinct from this session's killed `vwap_fade` (reversion to the VWAP
*mean*); PoC is the *mode* of the volume distribution. The proxy-volume engine is
a no-op for TXF, which reports true native exchange volume.
"""
