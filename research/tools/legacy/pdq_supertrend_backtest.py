"""Backtest Supertrend as a PDQ_cont direction prior.

This is a research-only audit.  It reuses the fixed canonical PDQ_cont event
set from the visual atlas and evaluates TradingView-style Supertrend
directions as low-frequency priors.  Supertrend values are joined from the
previous completed bar to avoid intrabar lookahead.
"""

from __future__ import annotations

import html
import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
BASE_TOOL = ROOT / "research/tools/pdq_tsi15_decomposition_audit.py"
ATLAS_DIR = ROOT / "outputs/liquidity_score/pdq_visual_atlas"
ROUTING_DIR = ROOT / "outputs/liquidity_score/pdq_full_routing_audit"
OUT_DIR = ROOT / "outputs/liquidity_score/pdq_supertrend_backtest"

ATR_PERIOD = 10
FACTOR = 3.0
TIMEFRAMES = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
}
FIXED_HOLDS = (180, 300, 600, 900)
BARRIER_GRID = ((8, 8, 300), (12, 8, 300), (16, 8, 600), (24, 12, 600))
COSTS = (2.0, 4.0, 6.0)


def load_base_tool():
    spec = importlib.util.spec_from_file_location("pdq_base_tool", BASE_TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load base PDQ tool: {BASE_TOOL}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pdq = load_base_tool()


def signed(values: pd.Series | np.ndarray, threshold: float = 0.0) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    out = np.zeros(arr.shape, dtype=np.int8)
    out[arr > threshold] = 1
    out[arr < -threshold] = -1
    return out


def rma(values: pd.Series, length: int) -> pd.Series:
    arr = values.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan, dtype=float)
    valid = np.isfinite(arr)
    valid_idx = np.flatnonzero(valid)
    if len(valid_idx) < length:
        return pd.Series(out, index=values.index)
    first_pos = valid_idx[length - 1]
    out[first_pos] = float(np.nanmean(arr[valid_idx[:length]]))
    for i in range(first_pos + 1, len(arr)):
        if not np.isfinite(arr[i]):
            out[i] = out[i - 1]
        elif not np.isfinite(out[i - 1]):
            out[i] = arr[i]
        else:
            out[i] = (out[i - 1] * (length - 1) + arr[i]) / length
    return pd.Series(out, index=values.index)


def compute_supertrend_bars(bars: pd.DataFrame, atr_period: int, factor: float) -> pd.DataFrame:
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    close = bars["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = rma(tr, atr_period)
    hl2 = (high + low) / 2.0
    basic_upper = hl2 + factor * atr
    basic_lower = hl2 - factor * atr

    final_upper = np.full(len(bars), np.nan, dtype=float)
    final_lower = np.full(len(bars), np.nan, dtype=float)
    pine_direction = np.full(len(bars), np.nan, dtype=float)
    supertrend = np.full(len(bars), np.nan, dtype=float)

    bu = basic_upper.to_numpy(dtype=float)
    bl = basic_lower.to_numpy(dtype=float)
    cl = close.to_numpy(dtype=float)
    atr_arr = atr.to_numpy(dtype=float)

    for i in range(len(bars)):
        if not np.isfinite(atr_arr[i]):
            continue
        if i == 0 or not np.isfinite(final_upper[i - 1]):
            final_upper[i] = bu[i]
            final_lower[i] = bl[i]
            pine_direction[i] = 1.0
            supertrend[i] = final_upper[i]
            continue

        final_upper[i] = (
            bu[i]
            if bu[i] < final_upper[i - 1] or cl[i - 1] > final_upper[i - 1]
            else final_upper[i - 1]
        )
        final_lower[i] = (
            bl[i]
            if bl[i] > final_lower[i - 1] or cl[i - 1] < final_lower[i - 1]
            else final_lower[i - 1]
        )

        prev_st = supertrend[i - 1]
        if not np.isfinite(prev_st):
            pine_direction[i] = 1.0
        elif np.isclose(prev_st, final_upper[i - 1], equal_nan=False):
            pine_direction[i] = -1.0 if cl[i] > final_upper[i] else 1.0
        else:
            pine_direction[i] = 1.0 if cl[i] < final_lower[i] else -1.0
        supertrend[i] = final_lower[i] if pine_direction[i] < 0 else final_upper[i]

    out = bars.copy()
    out["atr"] = atr
    out["supertrend"] = supertrend
    out["pine_direction"] = pine_direction
    # TradingView ta.supertrend direction < 0 is uptrend in the supplied script.
    out["st_dir"] = np.select(
        [out["pine_direction"] < 0, out["pine_direction"] > 0],
        [1, -1],
        default=0,
    ).astype(np.int8)
    out["st_flip"] = out["st_dir"].ne(out["st_dir"].shift(1)) & out["st_dir"].ne(0)
    return out


def build_supertrend_features(secbar: pd.DataFrame) -> pd.DataFrame:
    out = secbar[["sec", "day", "split", "mid_agg"]].copy()
    for tf_name, tf_s in TIMEFRAMES.items():
        bars = (
            secbar.assign(bar_start=(secbar["sec"] // tf_s) * tf_s)
            .groupby("bar_start", sort=True)
            .agg(
                open=("mid_agg", "first"),
                high=("mid_agg", "max"),
                low=("mid_agg", "min"),
                close=("mid_agg", "last"),
                last_sec=("sec", "last"),
            )
            .reset_index()
        )
        st = compute_supertrend_bars(bars, ATR_PERIOD, FACTOR)
        st = st.rename(
            columns={
                "bar_start": f"prev_bar_{tf_name}",
                "supertrend": f"st_{tf_name}",
                "st_dir": f"st_dir_{tf_name}",
                "st_flip": f"st_flip_{tf_name}",
                "atr": f"st_atr_{tf_name}",
            }
        )
        out[f"prev_bar_{tf_name}"] = (out["sec"] // tf_s - 1) * tf_s
        out = out.merge(
            st[
                [
                    f"prev_bar_{tf_name}",
                    f"st_{tf_name}",
                    f"st_dir_{tf_name}",
                    f"st_flip_{tf_name}",
                    f"st_atr_{tf_name}",
                ]
            ],
            on=f"prev_bar_{tf_name}",
            how="left",
            validate="many_to_one",
        )
        out[f"st_dir_{tf_name}"] = out[f"st_dir_{tf_name}"].fillna(0).astype(np.int8)
        out[f"st_flip_{tf_name}"] = out[f"st_flip_{tf_name}"].fillna(False).astype(bool)
    return out


def load_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    secbar = pdq.load_wide()
    secbar = pdq.add_pdq_features(secbar)
    st_features = build_supertrend_features(secbar)

    events = pd.read_csv(ROUTING_DIR / "events_with_vwap_proxy.csv")
    events = events.merge(
        st_features.drop(columns=["day", "split", "mid_agg"]),
        left_on="t0",
        right_on="sec",
        how="left",
        validate="many_to_one",
    )
    events = events.drop(columns=["sec"])
    events["e_TXF_dir"] = signed(events["e_TXF_t0"])
    return events, load_fixed_returns()


def load_fixed_returns() -> pd.DataFrame:
    usecols = ["event_id", "dt", "ret_common"]
    rows = []
    for chunk in pd.read_csv(ATLAS_DIR / "event_windows.csv.gz", usecols=usecols, chunksize=1_000_000):
        chunk = chunk[chunk["dt"].isin(FIXED_HOLDS)]
        if not chunk.empty:
            rows.append(chunk)
    if not rows:
        return pd.DataFrame({"event_id": []})
    fixed = pd.concat(rows, ignore_index=True)
    wide = fixed.pivot_table(index="event_id", columns="dt", values="ret_common", aggfunc="last")
    wide.columns = [f"ret_{int(col)}" for col in wide.columns]
    return wide.reset_index()


def add_direction_columns(events: pd.DataFrame) -> pd.DataFrame:
    out = events.copy()
    for tf_name in TIMEFRAMES:
        st = out[f"st_dir_{tf_name}"].fillna(0).astype(np.int8)
        c = out["entry_dir_C"].fillna(0).astype(np.int8)
        tsi = out["TSI15_dir"].fillna(0).astype(np.int8)
        etxf = out["e_TXF_dir"].fillna(0).astype(np.int8)
        out[f"st_c_state_{tf_name}"] = np.select(
            [(st != 0) & (st == c), (st != 0) & (st == -c)],
            ["ST_C_aligned", "ST_C_opposite"],
            default="ST_no_dir",
        )
        out[f"dir_ST_{tf_name}"] = st
        out[f"dir_ST_C_aligned_{tf_name}"] = np.where((st != 0) & (st == c), st, 0).astype(np.int8)
        out[f"dir_ST_C_opposite_ST_{tf_name}"] = np.where((st != 0) & (st == -c), st, 0).astype(np.int8)
        out[f"dir_ST_TSI_aligned_{tf_name}"] = np.where((st != 0) & (tsi == st), st, 0).astype(np.int8)
        out[f"dir_ST_C_TSI_all_aligned_{tf_name}"] = np.where(
            (st != 0) & (st == c) & (tsi == st), st, 0
        ).astype(np.int8)
        out[f"dir_ST_eTXF_aligned_{tf_name}"] = np.where((st != 0) & (etxf == st), st, 0).astype(np.int8)
        out[f"dir_ST_C_eTXF_all_aligned_{tf_name}"] = np.where(
            (st != 0) & (st == c) & (etxf == st), st, 0
        ).astype(np.int8)
        out[f"dir_ST_C_TSI_eTXF_all_aligned_{tf_name}"] = np.where(
            (st != 0) & (st == c) & (tsi == st) & (etxf == st), st, 0
        ).astype(np.int8)
        out[f"dir_ST_C_aligned_no_toxic_{tf_name}"] = np.where(
            (st != 0) & (st == c) & (~out["Toxicity_q70"].astype(bool)), st, 0
        ).astype(np.int8)
        out[f"dir_ST_reverse_proxy_{tf_name}"] = np.where(
            (st != 0) & (st == -c) & out["reverse_proxy_t0"].astype(bool), st, 0
        ).astype(np.int8)
        out[f"dir_ST_clean_proxy_{tf_name}"] = np.where(
            (st != 0) & (st == c) & out["clean_taker_proxy_t0"].astype(bool), st, 0
        ).astype(np.int8)
    return out


def first_passage_for_dir(events: pd.DataFrame, direction: pd.Series | np.ndarray, tp: float, sl: float) -> pd.DataFrame:
    d = np.asarray(direction, dtype=float)
    if tp == 8 and sl == 8:
        tp_time = np.where(d > 0, events["t_up8"], np.where(d < 0, events["t_down8"], np.nan))
        sl_time = np.where(d > 0, events["t_down8"], np.where(d < 0, events["t_up8"], np.nan))
        tp_first = np.isfinite(tp_time) & (~np.isfinite(sl_time) | (tp_time < sl_time))
        sl_first = np.isfinite(sl_time) & (~np.isfinite(tp_time) | (sl_time < tp_time))
        timeout = ~tp_first & ~sl_first
        return pd.DataFrame({"tp_first": tp_first, "sl_first": sl_first, "timeout": timeout})

    max_dir = np.where(d > 0, events["max_ret"], -events["min_ret"])
    min_dir = np.where(d > 0, events["min_ret"], -events["max_ret"])
    tp_hit = max_dir >= tp
    sl_hit = min_dir <= -sl
    tp_first = tp_hit & ~sl_hit
    sl_first = sl_hit & ~tp_hit
    timeout = ~(tp_first | sl_first)
    return pd.DataFrame({"tp_first": tp_first, "sl_first": sl_first, "timeout": timeout})


def summarize_pnl(events: pd.DataFrame, pnl_col: str, mfe_col: str) -> dict[str, float | int]:
    valid = events[pnl_col].notna()
    sub = events.loc[valid]
    if sub.empty:
        row: dict[str, float | int] = {
            "n": 0,
            "active_days": 0,
            "gross_mean": np.nan,
            "hit_rate": np.nan,
            "p25": np.nan,
            "p50": np.nan,
            "p75": np.nan,
            "mfe_ge8_rate": np.nan,
            "mfe_p75": np.nan,
            "mfe_p90": np.nan,
            "median_daily_pnl": np.nan,
            "top5_day_abs_share": np.nan,
            "drop_top3_day_gross_mean": np.nan,
        }
    else:
        pnl = sub[pnl_col].astype(float)
        daily = sub.groupby("day", sort=True)[pnl_col].sum()
        abs_total = float(daily.abs().sum())
        top5_share = float(daily.abs().nlargest(5).sum() / abs_total) if abs_total > 0 else np.nan
        keep_days = daily.abs().sort_values(ascending=False).iloc[3:].index
        drop_top3 = sub[sub["day"].isin(keep_days)][pnl_col]
        row = {
            "n": int(len(sub)),
            "active_days": int(sub["day"].nunique()),
            "gross_mean": float(pnl.mean()),
            "hit_rate": float((pnl > 0).mean()),
            "p25": float(pnl.quantile(0.25)),
            "p50": float(pnl.quantile(0.50)),
            "p75": float(pnl.quantile(0.75)),
            "mfe_ge8_rate": float((sub[mfe_col] >= 8.0).mean()),
            "mfe_p75": float(sub[mfe_col].quantile(0.75)),
            "mfe_p90": float(sub[mfe_col].quantile(0.90)),
            "median_daily_pnl": float(daily.median()),
            "top5_day_abs_share": top5_share,
            "drop_top3_day_gross_mean": float(drop_top3.mean()) if len(drop_top3) else np.nan,
        }
    for cost in COSTS:
        row[f"net_mean_cost{int(cost)}"] = row["gross_mean"] - cost if row["n"] else np.nan
    return row


def add_trade_path_metrics(events: pd.DataFrame, direction_col: str) -> pd.DataFrame:
    out = events.copy()
    d = out[direction_col].fillna(0).astype(float)
    valid = d != 0
    out["mfe_dir"] = np.nan
    out["mae_dir"] = np.nan
    out.loc[valid, "mfe_dir"] = np.where(d[valid] > 0, out.loc[valid, "max_ret"], -out.loc[valid, "min_ret"])
    out.loc[valid, "mae_dir"] = np.where(d[valid] > 0, out.loc[valid, "min_ret"], -out.loc[valid, "max_ret"])
    for hold in FIXED_HOLDS:
        ret_col = f"ret_{hold}"
        out[f"pnl_fixed_{hold}"] = np.where(valid, d * out[ret_col], np.nan)
    return out


def summarize_barrier(events: pd.DataFrame, direction_col: str, tp: float, sl: float) -> dict[str, float | int]:
    d = events[direction_col].fillna(0).astype(float)
    valid = d != 0
    sub = events.loc[valid].copy()
    if sub.empty:
        return {"n": 0, "active_days": 0}
    fp = first_passage_for_dir(sub, d.loc[valid], tp, sl)
    row: dict[str, float | int] = {
        "n": int(len(sub)),
        "active_days": int(sub["day"].nunique()),
        "tp_first": float(fp["tp_first"].mean()),
        "sl_first": float(fp["sl_first"].mean()),
        "timeout": float(fp["timeout"].mean()),
        "fp_edge": float(fp["tp_first"].mean() - fp["sl_first"].mean()),
        "clean_share": float(sub["trade_path_label_C"].eq("clean_continuation").mean()),
        "wrong_way_share": float(sub["trade_path_label_C"].eq("wrong_way_first").mean()),
        "two_sided_share": float(sub["trade_path_label_C"].eq("two_sided_chop").mean()),
    }
    if tp == 8 and sl == 8:
        pnl = np.where(fp["tp_first"], tp, np.where(fp["sl_first"], -sl, sub["final_ret"] * d.loc[valid].to_numpy()))
        row["tb_gross_mean"] = float(np.nanmean(pnl))
        for cost in COSTS:
            row[f"tb_net_mean_cost{int(cost)}"] = row["tb_gross_mean"] - cost
    else:
        row["tb_gross_mean"] = np.nan
        for cost in COSTS:
            row[f"tb_net_mean_cost{int(cost)}"] = np.nan
    return row


def build_rule_specs() -> list[tuple[str, str, str]]:
    specs: list[tuple[str, str, str]] = [("BASE_signC", "entry_dir_C", "baseline")]
    for tf_name in TIMEFRAMES:
        specs.extend(
            [
                (f"ST_{tf_name}_all", f"dir_ST_{tf_name}", "supertrend_direction"),
                (f"ST_{tf_name}_C_aligned", f"dir_ST_C_aligned_{tf_name}", "continuation_filter"),
                (f"ST_{tf_name}_C_opposite_trade_ST", f"dir_ST_C_opposite_ST_{tf_name}", "wrong_way_reverse_candidate"),
                (f"ST_{tf_name}_TSI_aligned", f"dir_ST_TSI_aligned_{tf_name}", "slow_prior_confirmation"),
                (f"ST_{tf_name}_C_TSI_all_aligned", f"dir_ST_C_TSI_all_aligned_{tf_name}", "strict_continuation"),
                (f"ST_{tf_name}_eTXF_aligned", f"dir_ST_eTXF_aligned_{tf_name}", "txf_residual_confirmation"),
                (f"ST_{tf_name}_C_eTXF_all_aligned", f"dir_ST_C_eTXF_all_aligned_{tf_name}", "pdq_txf_continuation"),
                (f"ST_{tf_name}_C_TSI_eTXF_all_aligned", f"dir_ST_C_TSI_eTXF_all_aligned_{tf_name}", "strict_all_confirmation"),
                (f"ST_{tf_name}_C_aligned_no_toxic", f"dir_ST_C_aligned_no_toxic_{tf_name}", "execution_veto"),
                (f"ST_{tf_name}_clean_proxy", f"dir_ST_clean_proxy_{tf_name}", "prior_clean_proxy"),
                (f"ST_{tf_name}_reverse_proxy", f"dir_ST_reverse_proxy_{tf_name}", "prior_reverse_proxy"),
            ]
        )
    return specs


def fixed_hold_summary(events: pd.DataFrame, specs: list[tuple[str, str, str]]) -> pd.DataFrame:
    rows = []
    for rule, dir_col, family in specs:
        trade = add_trade_path_metrics(events, dir_col)
        for split, split_df in trade.groupby("split", sort=True):
            for hold in FIXED_HOLDS:
                row = {
                    "rule": rule,
                    "family": family,
                    "split": split,
                    "hold_s": hold,
                }
                row.update(summarize_pnl(split_df, f"pnl_fixed_{hold}", "mfe_dir"))
                rows.append(row)
    return pd.DataFrame(rows)


def barrier_summary(events: pd.DataFrame, specs: list[tuple[str, str, str]]) -> pd.DataFrame:
    rows = []
    for rule, dir_col, family in specs:
        for split, split_df in events.groupby("split", sort=True):
            for tp, sl, maxhold in BARRIER_GRID:
                row = {
                    "rule": rule,
                    "family": family,
                    "split": split,
                    "tp": tp,
                    "sl": sl,
                    "maxhold_s": maxhold,
                    "barrier_note": "exact timing and PnL only for 8/8; larger barriers use max/min feasibility proxy",
                }
                row.update(summarize_barrier(split_df, dir_col, tp, sl))
                rows.append(row)
    return pd.DataFrame(rows)


def direction_distribution(events: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for tf_name in TIMEFRAMES:
        col = f"st_dir_{tf_name}"
        state_col = f"st_c_state_{tf_name}"
        for (split, state), g in events.groupby(["split", state_col], sort=True):
            rows.append(
                {
                    "timeframe": tf_name,
                    "split": split,
                    "state": state,
                    "n": int(len(g)),
                    "share": float(len(g) / len(events[events["split"].eq(split)])),
                    "clean_share": float(g["trade_path_label_C"].eq("clean_continuation").mean()),
                    "wrong_way_share": float(g["trade_path_label_C"].eq("wrong_way_first").mean()),
                    "two_sided_share": float(g["trade_path_label_C"].eq("two_sided_chop").mean()),
                    "st_long_share": float(g[col].eq(1).mean()),
                    "st_short_share": float(g[col].eq(-1).mean()),
                }
            )
    return pd.DataFrame(rows)


def hour_summary(events: pd.DataFrame, specs: list[tuple[str, str, str]]) -> pd.DataFrame:
    selected = [
        ("BASE_signC", "entry_dir_C", "baseline"),
        ("ST_15m_C_aligned", "dir_ST_C_aligned_15m", "continuation_filter"),
        ("ST_15m_C_opposite_trade_ST", "dir_ST_C_opposite_ST_15m", "wrong_way_reverse_candidate"),
        ("ST_5m_C_aligned", "dir_ST_C_aligned_5m", "continuation_filter"),
    ]
    rows = []
    for rule, dir_col, family in selected:
        trade = add_trade_path_metrics(events, dir_col)
        for (split, hour), g in trade.groupby(["split", "hour_tpe"], sort=True):
            row = {"rule": rule, "family": family, "split": split, "hour_tpe": int(hour), "hold_s": 300}
            row.update(summarize_pnl(g, "pnl_fixed_300", "mfe_dir"))
            rows.append(row)
    return pd.DataFrame(rows)


def write_html(tables: dict[str, pd.DataFrame], meta: dict[str, object]) -> None:
    css = (
        "body{font-family:Arial;margin:24px;color:#17202a}"
        "table{border-collapse:collapse;font-size:12px;margin:14px 0}"
        "th,td{border:1px solid #ddd;padding:4px 6px;text-align:right}"
        "th{text-align:left;background:#eef2f6}.note{max-width:1120px;line-height:1.5}"
    )
    body = [
        "<!doctype html><html><head><meta charset='utf-8'><title>PDQ Supertrend Backtest</title>",
        f"<style>{css}</style></head><body>",
        "<h1>PDQ Supertrend Backtest</h1>",
        "<p class='note'>Supertrend is evaluated as a completed-bar low-frequency prior over the fixed PDQ_cont canonical event set. PnL is in TAIFEX points. Cost columns subtract fixed round-trip costs in points. This is signal/path research, not a production fill simulation.</p>",
        f"<pre>{html.escape(json.dumps(meta, indent=2, sort_keys=True))}</pre>",
    ]
    for name, df in tables.items():
        body.append(f"<h2>{html.escape(name)}</h2>")
        body.append(df.round(4).to_html(index=False, escape=True))
    body.append("</body></html>")
    (OUT_DIR / "index.html").write_text("\n".join(body))


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    events, fixed = load_inputs()
    events = events.merge(fixed, on="event_id", how="left", validate="one_to_one")
    events = add_direction_columns(events)
    specs = build_rule_specs()

    direction = direction_distribution(events)
    fixed_summary = fixed_hold_summary(events, specs)
    barriers = barrier_summary(events, specs)
    hours = hour_summary(events, specs)

    events.to_csv(OUT_DIR / "supertrend_event_features.csv", index=False)
    direction.to_csv(OUT_DIR / "supertrend_direction_distribution.csv", index=False)
    fixed_summary.to_csv(OUT_DIR / "supertrend_fixed_hold_summary.csv", index=False)
    barriers.to_csv(OUT_DIR / "supertrend_barrier_summary.csv", index=False)
    hours.to_csv(OUT_DIR / "supertrend_hour_summary.csv", index=False)

    meta = {
        "source_secbar": str(pdq.DATA_PATH.relative_to(ROOT)),
        "source_events": str((ROUTING_DIR / "events_with_vwap_proxy.csv").relative_to(ROOT)),
        "source_windows": str((ATLAS_DIR / "event_windows.csv.gz").relative_to(ROOT)),
        "events": int(len(events)),
        "is_period": "2026-03-03..2026-04-30",
        "oos_period": "2026-05-01..2026-06-13",
        "indicator": "TradingView-style Supertrend",
        "atr_period": ATR_PERIOD,
        "factor": FACTOR,
        "timeframes": TIMEFRAMES,
        "bar_alignment": "previous completed bar only; no intrabar lookahead",
        "price_unit": "TAIFEX points",
        "costs_points": list(COSTS),
        "barrier_note": "8/8 has exact first-touch timing; larger barriers use max/min feasibility proxy because atlas stores exact +/-8 times only",
        "fill_model": "taker signal/path proxy on common mid; not bid/ask fill simulation",
    }
    (OUT_DIR / "metadata.json").write_text(json.dumps(meta, indent=2, sort_keys=True))
    write_html(
        {
            "Direction Distribution": direction,
            "Fixed Hold Summary": fixed_summary,
            "Barrier Summary": barriers,
            "Hour Summary": hours,
        },
        meta,
    )


if __name__ == "__main__":
    main()
