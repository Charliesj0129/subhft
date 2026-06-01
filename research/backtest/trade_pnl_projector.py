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


def project_trade_pnl_from_position_series(
    positions: Any,
    prices: Any,
    *,
    price_scale: float = 1.0,
    force_flat_at_end: bool = True,
) -> list[float]:
    """Round 38: derive per-trip PnL from a (positions, prices) series.

    Mirrors :func:`project_trade_pnl` for engines that don't produce a
    discrete fill list — most notably the taker path that wraps
    ``hft_native_runner``.  Goal §5.1 requires per-round-trip PnL on the
    taker side too, otherwise ``edge_per_round_trip`` / trade-axis
    sub-gates silently fall back to daily aggregates.

    Algorithm: every step ``i`` whose position differs from ``i-1``
    is split into ``abs(delta)`` unit-sized synthetic fills priced at
    ``prices[i]`` and fed through the same FIFO matcher.  This is the
    minimum reconstruction that respects FIFO without changing the
    runner.

    ``prices`` may be raw scaled int (live tick path: x10000) or
    points (offline path: x1).  Pass ``price_scale`` accordingly —
    ``1.0`` means prices are already in points.

    Round 41: ``force_flat_at_end`` (default True) handles residual
    inventory per goal 驗證標準 §3 — "若有未平倉殘倉，必須先用 MtM
    或 force-flat rule 納入 PnL".  When the series ends with a non-zero
    position, a synthetic close transition (positions[-1] -> 0 at
    ``prices[-1]``) is appended before FIFO matching so the residual
    is realized into trips rather than silently dropped (which would
    inflate edge by hiding inventory losses).  Pass ``False`` only
    when the caller is supplying residual MtM via a different path.

    Defensive: returns ``[]`` for mismatched-length arrays, empty
    arrays, or arrays of length<2 (no transitions possible).  Never
    raises on shape — the caller (taker_engine) sweeps many runner
    outputs and a single weird series must not abort the batch.
    """
    if positions is None or prices is None:
        return []
    try:
        n_pos = len(positions)
        n_px = len(prices)
    except TypeError:
        return []
    if n_pos < 2 or n_pos != n_px:
        return []

    fills: list[dict[str, Any]] = []
    scale = float(price_scale) if price_scale else 1.0
    for i in range(1, n_pos):
        try:
            delta = int(positions[i]) - int(positions[i - 1])
        except (TypeError, ValueError):
            continue
        if delta == 0:
            continue
        side = "buy" if delta > 0 else "sell"
        try:
            price_raw = float(prices[i])
        except (TypeError, ValueError):
            continue
        # Re-scale so project_trade_pnl's divisor lands on points.
        fills.extend(
            {"side": side, "price": price_raw} for _ in range(abs(delta))
        )
    if force_flat_at_end:
        try:
            end_pos = int(positions[-1])
        except (TypeError, ValueError):
            end_pos = 0
        if end_pos != 0:
            try:
                last_price = float(prices[-1])
            except (TypeError, ValueError):
                last_price = None
            if last_price is not None:
                close_side = "sell" if end_pos > 0 else "buy"
                fills.extend(
                    {"side": close_side, "price": last_price}
                    for _ in range(abs(end_pos))
                )
    return project_trade_pnl(fills, price_scale=scale)


def project_trade_pnl(
    fills: list[dict[str, Any]],
    *,
    price_scale: float = _DEFAULT_PRICE_SCALE,
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
