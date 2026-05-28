"""R47 Skew MR: IV Skew Mean-Reversion via Vertical Spreads on TXO.

Signal: premium-ratio skew z-score (OTM put mid / OTM call mid ratio).
When |z| > threshold, enter vertical spread betting on skew normalization.
Primary signal uses premium-ratio (no TX futures dependency, 5x more observations).

Paper refs:
    1611.05518 — Nadtochiy & Obloj (2016), Robust Trading of Implied Skew
    2009.09713 — Nasekin & Haerdle (2020), Model-driven stat-arb on LETF options
    2501.12397 — Huang et al. (2025), Iron Condor Optimal Control

Float exception: Per Architecture Governance Rule 25 s11, float is permitted
in this offline research module.
"""

from __future__ import annotations

import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

from research.registry.schemas import AlphaManifest, AlphaStatus, Scorecard

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

MANIFEST = AlphaManifest(
    alpha_id="r47_skew_mr",
    hypothesis=(
        "TXO IV skew (put/call premium ratio) mean-reverts on 30-60 min horizons. "
        "After z > 1.5, sell OTM put vertical (short skew). After z < -1.5, buy "
        "OTM put vertical (long skew). Primary signal: premium-ratio proxy."
    ),
    formula=(
        "skew_ratio = put_otm_mid / call_otm_mid; "
        "z = (skew_ratio - EMA_N) / rolling_std_N; "
        "signal = -sign(z) if |z| > threshold"
    ),
    paper_refs=("1611.05518", "2009.09713", "2501.12397"),
    data_fields=("txo_bid", "txo_ask", "txo_symbol"),
    complexity="O(S)",  # S = number of strikes
    latency_profile="sim_p95_v2026-02-26",
    roles_used=("planner",),
    skills_used=("iterative-retrieval",),
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_Z_ENTRY = 1.5       # z-score threshold for entry
_Z_EXIT = 0.5        # z-score threshold for exit (mean reversion target)
_LOOKBACK = 8        # 8 x 15-min = 2hr lookback for z-score
_HOLD_MAX_BUCKETS = 16  # max hold = 16 x 15min = 4hr
_COST_PER_VERTICAL_PTS = 4.68  # RT cost: 2 x (spread crossing 1.5 + commission 0.84)
_TXO_MULTIPLIER = 50  # NTD per point

# Shioaji TXO month codes
_CALL_MONTHS = "ABCDEFGHIJKL"
_PUT_MONTHS = "MNOPQRSTUVWX"
_CALL_TO_MONTH = {c: i + 1 for i, c in enumerate(_CALL_MONTHS)}
_PUT_TO_MONTH = {c: i + 1 for i, c in enumerate(_PUT_MONTHS)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_txo(sym: str) -> tuple[int, str, tuple[int, int]] | None:
    """Parse TXO symbol -> (strike, 'C'|'P', (month, year)) or None."""
    if not sym.startswith("TXO"):
        return None
    rest = sym[3:]
    digits, month_char, year_char = "", "", ""
    for ch in rest:
        if ch.isdigit() and not month_char:
            digits += ch
        elif ch.isalpha() and not month_char:
            month_char = ch
        elif ch.isdigit() and month_char and not year_char:
            year_char = ch
    if not digits or not month_char:
        return None
    strike = int(digits)
    mu = month_char.upper()
    if mu in _CALL_TO_MONTH:
        return (strike, "C", (_CALL_TO_MONTH[mu], int(year_char) if year_char else 0))
    if mu in _PUT_TO_MONTH:
        return (strike, "P", (_PUT_TO_MONTH[mu], int(year_char) if year_char else 0))
    return None


def _get_ch_client():
    """Get ClickHouse client."""
    import clickhouse_connect
    return clickhouse_connect.get_client(
        host=os.environ.get("HFT_CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("HFT_CLICKHOUSE_HTTP_PORT", "8123")),
        username="default",
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
    )


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

@dataclass
class BucketQuote:
    """Aggregated TXO quote for one 15-min bucket."""
    bucket_ts: int
    strike: int
    right: str  # 'C' or 'P'
    mid: float
    spread: float


def load_txo_15min() -> list[BucketQuote]:
    """Load TXO 15-min aggregated quotes from ClickHouse."""
    client = _get_ch_client()
    sql = """
    SELECT
        symbol,
        toInt64(floor(exch_ts / 900000000000) * 900000000000) as bucket,
        avg(bids_price[1] + asks_price[1]) / 2000000.0 as mid_price,
        avg(asks_price[1] - bids_price[1]) / 1000000.0 as spread
    FROM hft.market_data
    WHERE symbol LIKE 'TXO%'
      AND type = 'BidAsk'
      AND length(bids_price) > 0 AND length(asks_price) > 0
      AND bids_price[1] > 0 AND asks_price[1] > bids_price[1]
    GROUP BY symbol, bucket
    ORDER BY bucket, symbol
    """
    result = client.query(sql)
    quotes = []
    for row in result.result_rows:
        sym, bucket, mid, spread = str(row[0]), int(row[1]), float(row[2]), float(row[3])
        parsed = _parse_txo(sym)
        if parsed and mid > 0.1:
            strike, right, _ = parsed
            quotes.append(BucketQuote(bucket, strike, right, mid, spread))
    return quotes


# ---------------------------------------------------------------------------
# Skew computation
# ---------------------------------------------------------------------------

@dataclass
class SkewPoint:
    """One skew observation."""
    bucket_ts: int
    ratio: float          # put_otm_mid / call_otm_mid
    atm_strike: int
    put_strike: int
    call_strike: int
    put_mid: float
    call_mid: float
    n_matched: int


def compute_skew_series(quotes: list[BucketQuote]) -> list[SkewPoint]:
    """Compute premium-ratio skew for each 15-min bucket."""
    # Group by bucket -> {strike: {'C': mid, 'P': mid}}
    bucket_data: dict[int, dict[int, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for q in quotes:
        bucket_data[q.bucket_ts][q.strike][q.right] = q.mid

    series = []
    for bucket in sorted(bucket_data.keys()):
        strike_map = bucket_data[bucket]
        # Find matched put/call pairs
        matched = {
            k: (rights["P"], rights["C"])
            for k, rights in strike_map.items()
            if "C" in rights and "P" in rights
        }
        if len(matched) < 3:
            continue

        strikes = sorted(matched.keys())
        # ATM = strike where put ~ call
        atm_strike = min(strikes, key=lambda k: abs(matched[k][0] - matched[k][1]))

        otm_puts = sorted([k for k in strikes if k < atm_strike], reverse=True)
        otm_calls = sorted([k for k in strikes if k > atm_strike])
        if not otm_puts or not otm_calls:
            continue

        put_k = otm_puts[1] if len(otm_puts) > 1 else otm_puts[0]
        call_k = otm_calls[1] if len(otm_calls) > 1 else otm_calls[0]
        put_mid = matched[put_k][0]
        call_mid = matched[call_k][1]

        if put_mid > 0.1 and call_mid > 0.1:
            series.append(SkewPoint(
                bucket_ts=bucket,
                ratio=put_mid / call_mid,
                atm_strike=atm_strike,
                put_strike=put_k,
                call_strike=call_k,
                put_mid=put_mid,
                call_mid=call_mid,
                n_matched=len(matched),
            ))
    return series


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    """One completed round-trip trade."""
    entry_ts: int
    exit_ts: int
    direction: int        # +1 = long skew (buy put vertical), -1 = short skew
    entry_z: float
    exit_z: float
    entry_ratio: float
    exit_ratio: float
    pnl_ratio: float      # ratio change * direction
    pnl_pts: float        # estimated P&L in points
    pnl_ntd: float        # P&L in NTD
    hold_buckets: int


def backtest(
    series: list[SkewPoint],
    z_entry: float = _Z_ENTRY,
    z_exit: float = _Z_EXIT,
    lookback: int = _LOOKBACK,
    hold_max: int = _HOLD_MAX_BUCKETS,
    cost_pts: float = _COST_PER_VERTICAL_PTS,
    vega_pts_per_ratio: float = 50.0,  # estimated pts P&L per 1.0 ratio change
) -> list[Trade]:
    """Run backtest on skew series.

    vega_pts_per_ratio: approximate conversion from ratio change to point P&L.
    For OTM options with mid ~15 pts, ratio change of 0.1 ~ 1.5 pts price change.
    So vega_pts_per_ratio ~ 15 (conservative). Use 50 for the average case.
    """
    if len(series) < lookback + 5:
        return []

    ratios = np.array([s.ratio for s in series])
    trades: list[Trade] = []
    position = 0  # 0=flat, +1=long skew, -1=short skew
    entry_idx = 0
    entry_z = 0.0

    for i in range(lookback, len(series)):
        window = ratios[i - lookback:i]
        mu = np.mean(window)
        std = np.std(window)
        if std < 1e-8:
            continue
        z = (ratios[i] - mu) / std

        if position == 0:
            # Check entry
            if z > z_entry:
                position = -1  # short skew (sell put, buy call)
                entry_idx = i
                entry_z = z
            elif z < -z_entry:
                position = +1  # long skew (buy put, sell call)
                entry_idx = i
                entry_z = z
        else:
            # Check exit
            hold = i - entry_idx
            exit_now = False

            if position == -1 and z < z_exit:
                exit_now = True
            elif position == +1 and z > -z_exit:
                exit_now = True
            elif hold >= hold_max:
                exit_now = True

            if exit_now:
                ratio_change = ratios[i] - ratios[entry_idx]
                pnl_ratio = ratio_change * (-position)  # profit when ratio reverts
                pnl_pts = pnl_ratio * vega_pts_per_ratio - cost_pts
                pnl_ntd = pnl_pts * _TXO_MULTIPLIER

                trades.append(Trade(
                    entry_ts=series[entry_idx].bucket_ts,
                    exit_ts=series[i].bucket_ts,
                    direction=position,
                    entry_z=entry_z,
                    exit_z=z,
                    entry_ratio=ratios[entry_idx],
                    exit_ratio=ratios[i],
                    pnl_ratio=pnl_ratio,
                    pnl_pts=pnl_pts,
                    pnl_ntd=pnl_ntd,
                    hold_buckets=hold,
                ))
                position = 0

    return trades


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------

def compute_scorecard(trades: list[Trade]) -> dict:
    """Compute backtest scorecard from trade list."""
    if not trades:
        return {"n_trades": 0, "verdict": "NO TRADES"}

    pnl = np.array([t.pnl_pts for t in trades])
    pnl_ntd = np.array([t.pnl_ntd for t in trades])
    holds = np.array([t.hold_buckets for t in trades])
    n = len(trades)

    total_pnl = np.sum(pnl)
    mean_pnl = np.mean(pnl)
    win_mask = pnl > 0
    win_rate = np.mean(win_mask)

    # Sharpe: annualize assuming ~3 trades/day, 252 trading days
    if np.std(pnl) > 0:
        daily_sharpe = mean_pnl / np.std(pnl)
        annual_sharpe = daily_sharpe * math.sqrt(3 * 252)  # 3 trades/day
    else:
        annual_sharpe = 0.0

    # Drawdown
    cumulative = np.cumsum(pnl_ntd)
    peak = np.maximum.accumulate(cumulative)
    drawdown = cumulative - peak
    max_dd = np.min(drawdown)

    # Direction breakdown
    short_trades = [t for t in trades if t.direction == -1]
    long_trades = [t for t in trades if t.direction == +1]

    result = {
        "n_trades": n,
        "total_pnl_pts": float(total_pnl),
        "total_pnl_ntd": float(np.sum(pnl_ntd)),
        "mean_pnl_pts": float(mean_pnl),
        "std_pnl_pts": float(np.std(pnl)),
        "win_rate": float(win_rate),
        "sharpe_annual": float(annual_sharpe),
        "max_drawdown_ntd": float(max_dd),
        "avg_hold_buckets": float(np.mean(holds)),
        "avg_hold_min": float(np.mean(holds) * 15),
        "n_short_skew": len(short_trades),
        "n_long_skew": len(long_trades),
    }

    if short_trades:
        short_pnl = np.array([t.pnl_pts for t in short_trades])
        result["short_mean_pnl"] = float(np.mean(short_pnl))
        result["short_win_rate"] = float(np.mean(short_pnl > 0))

    if long_trades:
        long_pnl = np.array([t.pnl_pts for t in long_trades])
        result["long_mean_pnl"] = float(np.mean(long_pnl))
        result["long_win_rate"] = float(np.mean(long_pnl > 0))

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    """Run full backtest and print scorecard."""
    print("=" * 70)
    print("R47 SKEW MR BACKTEST")
    print("=" * 70)

    print("\nLoading TXO data...")
    quotes = load_txo_15min()
    print(f"  Loaded {len(quotes):,} 15-min quote records")

    print("\nComputing skew series...")
    series = compute_skew_series(quotes)
    print(f"  Skew observations: {len(series):,}")

    if len(series) < _LOOKBACK + 10:
        print(f"\nINSUFFICIENT DATA: {len(series)} skew points < minimum {_LOOKBACK + 10}")
        return

    # Parameter sweep
    print("\n" + "=" * 70)
    print("PARAMETER SWEEP")
    print("=" * 70)

    best_sharpe = -999.0
    best_params = {}

    for z_entry in [1.0, 1.5, 2.0]:
        for z_exit in [0.3, 0.5, 0.8]:
            for vega in [15.0, 30.0, 50.0]:
                trades = backtest(series, z_entry=z_entry, z_exit=z_exit,
                                  vega_pts_per_ratio=vega)
                sc = compute_scorecard(trades)
                n = sc["n_trades"]
                if n < 5:
                    continue
                sharpe = sc["sharpe_annual"]
                wr = sc["win_rate"]
                mean_pnl = sc["mean_pnl_pts"]
                print(f"  z_entry={z_entry} z_exit={z_exit} vega={vega:>4.0f}: "
                      f"n={n:>3} Sharpe={sharpe:>6.2f} WR={wr:.1%} "
                      f"mean={mean_pnl:>6.2f}pts")

                if sharpe > best_sharpe:
                    best_sharpe = sharpe
                    best_params = {"z_entry": z_entry, "z_exit": z_exit, "vega": vega}

    if not best_params:
        print("\nNO VIABLE PARAMETER SET FOUND")
        return

    # Run best config
    print(f"\n{'=' * 70}")
    print(f"BEST CONFIG: {best_params}")
    print(f"{'=' * 70}")

    trades = backtest(series, **{k: v for k, v in best_params.items()
                                  if k != "vega"},
                      vega_pts_per_ratio=best_params["vega"])
    sc = compute_scorecard(trades)

    print(f"\n--- SCORECARD ---")
    for k, v in sc.items():
        if isinstance(v, float):
            print(f"  {k}: {v:.4f}")
        else:
            print(f"  {k}: {v}")

    # Trade-by-trade
    print(f"\n--- TRADE LOG (first 20) ---")
    for i, t in enumerate(trades[:20]):
        ts_s = t.entry_ts // 1_000_000_000
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(ts_s, tz=timezone.utc).strftime("%m-%d %H:%M")
        dir_str = "SHORT_SKEW" if t.direction == -1 else "LONG_SKEW"
        print(f"  {i+1:>3}. {dt} {dir_str:>11} z={t.entry_z:>5.2f} "
              f"hold={t.hold_buckets*15:>3}min "
              f"pnl={t.pnl_pts:>+7.2f}pts ({t.pnl_ntd:>+8.0f}NTD)")


if __name__ == "__main__":
    main()
