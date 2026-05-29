"""Round-trip / FIFO matched trade PnL projector (Round 24, goal §5.1).

Goal 驗證標準 §1 defines edge as the per-round-trip net PnL.  The
trade-axis sub-gates (``trade_concentration``, ``outlier_trade_removal``,
``edge_per_round_trip``) consume ``BacktestResult.trade_pnl`` — a flat
``list[float]`` of per-trip PnL in points.  Without this field they
silently fall back to ``daily_pnl``, which dilutes single-trade
dominance signals.

``MakerEngine._compute_fifo_pnl`` already implements FIFO matching but
collapses the result into a scalar.  This projector mirrors its logic
and returns the per-trip list so engines can populate ``trade_pnl``
without restructuring the engine itself.

Fill schema (matches ``MakerEngine._compute_fifo_pnl``):
    fills: list[dict] with keys
        * ``side``: "buy" | "sell"
        * ``price``: scaled int (x1_000_000) per maker_engine convention
        * (additional keys ignored)

Price scale is intentionally configurable: the live tick path stores
prices x10_000 (CLAUDE.md hot-path law §4), the offline maker engine
stores them x1_000_000 (golden parquet convention).  Pass the scale
the caller's fills use.

Residual handling: any unmatched fills at the end remain unmatched.
This matches ``_compute_fifo_pnl`` — residual MtM is the engine's
``residual_mtm_pts`` job, not the trade list's.
"""

from __future__ import annotations

from typing import Any

_DEFAULT_PRICE_SCALE = 1_000_000


def project_trade_pnl(
    fills: list[dict[str, Any]],
    *,
    price_scale: int = _DEFAULT_PRICE_SCALE,
) -> list[float]:
    """Return per-round-trip PnL in points via FIFO matching.

    Mirrors ``MakerEngine._compute_fifo_pnl`` but emits the per-trip
    list instead of the realized total.  Sum of the returned list
    equals that helper's ``gross_pnl_pts`` for identical input — the
    test suite asserts this invariant.

    An empty / None ``fills`` returns ``[]`` (the trade-axis gates
    treat that as "no trade data — skip").
    """
    if not fills:
        return []

    buy_q: list[float] = []
    sell_q: list[float] = []
    trips: list[float] = []
    scale = float(price_scale)

    for f in fills:
        price_pts = float(f["price"]) / scale
        if f["side"] == "buy":
            if sell_q:
                sp = sell_q.pop(0)
                trips.append(sp - price_pts)
            else:
                buy_q.append(price_pts)
        else:
            if buy_q:
                bp = buy_q.pop(0)
                trips.append(price_pts - bp)
            else:
                sell_q.append(price_pts)

    return trips
