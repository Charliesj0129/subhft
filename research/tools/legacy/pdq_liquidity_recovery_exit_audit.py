"""Audit liquidity-recovery exits for PDQ + Supertrend candidate entries.

Entry uses t0-available fields only.  Exit is triggered after entry when book
thickness/liquidity improves: D5 recovers, zLogL improves, and/or spread
compresses.  This avoids using post-entry liquidity_state as an entry gate.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
EVENTS_PATH = ROOT / "outputs/liquidity_score/pdq_supertrend_backtest/supertrend_event_features.csv"
WINDOWS_PATH = ROOT / "outputs/liquidity_score/pdq_visual_atlas/event_windows.csv.gz"
OUT_DIR = ROOT / "outputs/liquidity_score/pdq_liquidity_recovery_exit"
COST_POINTS = 4.0
HOURS = {0, 9, 10, 12, 13, 16, 17, 18, 19, 21, 22}


def add_quantiles(events: pd.DataFrame) -> pd.DataFrame:
    out = events.copy()
    for col, prefix in [
        ("RVExp_t0", "rvexp"),
        ("spread_t0", "spread"),
        ("abs_C60", "abs_c"),
    ]:
        for q in (60, 70, 80, 90):
            out[f"{prefix}_q{q}"] = out.groupby("split")[col].transform(
                lambda s, qq=q: s.quantile(qq / 100.0)
            )
    return out


def build_entries(events: pd.DataFrame) -> pd.DataFrame:
    events = add_quantiles(events)
    rules = []
    base = (
        events["hour_tpe"].isin(HOURS)
        & events["dir_ST_C_eTXF_all_aligned_1m"].ne(0)
        & events["VWAP300_C_state"].eq("VWAP_C_aligned")
        & events["RVExp_t0"].ge(events["rvexp_q70"])
        & events["spread_t0"].le(events["spread_q90"])
    )
    variants = {
        "entry_rt_base_st1_c_etxf_vwap_rv70_spread90": base,
        "entry_rt_base_no_toxic": base & ~events["Toxicity_q70"].astype(bool),
        "entry_rt_base_absC60": base & events["abs_C60"].ge(events["abs_c_q60"]),
        "entry_rt_base_low_spike": base & events["C_shape_bucket"].astype(str).str.startswith("low_spike"),
    }
    for name, mask in variants.items():
        sub = events.loc[mask].copy()
        sub["entry_rule"] = name
        sub["direction"] = sub["dir_ST_C_eTXF_all_aligned_1m"].astype(np.int8)
        rules.append(sub)
    return pd.concat(rules, ignore_index=True)


def load_windows(event_ids: set[str]) -> pd.DataFrame:
    cols = ["event_id", "dt", "ret_common", "D5", "zLogL", "spread"]
    chunks = []
    for chunk in pd.read_csv(WINDOWS_PATH, usecols=cols, chunksize=1_000_000):
        chunk = chunk[chunk["event_id"].isin(event_ids)]
        if not chunk.empty:
            chunks.append(chunk)
    if not chunks:
        return pd.DataFrame(columns=cols)
    out = pd.concat(chunks, ignore_index=True)
    return out.sort_values(["event_id", "dt"], kind="mergesort").reset_index(drop=True)


def fixed_summary(entries: pd.DataFrame, hold_s: int) -> pd.DataFrame:
    rows = []
    ret_col = f"ret_{hold_s}"
    for (rule, split), g in entries.groupby(["entry_rule", "split"], sort=True):
        pnl = g["direction"].to_numpy(dtype=float) * g[ret_col].to_numpy(dtype=float)
        rows.append(summarize(rule, split, f"fixed_{hold_s}", g, pnl, np.full(len(g), hold_s), np.array(["fixed"] * len(g), dtype=object)))
    return pd.DataFrame(rows)


def summarize(rule: str, split: str, exit_rule: str, entries: pd.DataFrame, pnl: np.ndarray, hold: np.ndarray, reasons: np.ndarray) -> dict[str, float | int | str]:
    valid = np.isfinite(pnl)
    sub = entries.loc[valid].copy()
    pnl = pnl[valid]
    hold = hold[valid]
    reasons = reasons[valid]
    if len(sub) == 0:
        return {
            "entry_rule": rule,
            "split": split,
            "exit_rule": exit_rule,
            "n": 0,
            "active_days": 0,
        }
    daily = pd.Series(pnl).groupby(sub["day"].to_numpy(), sort=True).sum()
    abs_total = float(daily.abs().sum())
    keep_days = daily.abs().sort_values(ascending=False).iloc[3:].index
    drop = pnl[np.isin(sub["day"].to_numpy(), keep_days)]
    return {
        "entry_rule": rule,
        "split": split,
        "exit_rule": exit_rule,
        "n": int(len(sub)),
        "active_days": int(sub["day"].nunique()),
        "gross_mean": float(np.mean(pnl)),
        "net_cost4": float(np.mean(pnl) - COST_POINTS),
        "hit_rate": float(np.mean(pnl > 0)),
        "p25": float(np.quantile(pnl, 0.25)),
        "p50": float(np.quantile(pnl, 0.50)),
        "p75": float(np.quantile(pnl, 0.75)),
        "avg_hold_s": float(np.mean(hold)),
        "p50_hold_s": float(np.quantile(hold, 0.50)),
        "top5_day_abs_share": float(daily.abs().nlargest(5).sum() / abs_total) if abs_total > 0 else np.nan,
        "drop_top3_gross_mean": float(np.mean(drop)) if len(drop) else np.nan,
        "drop_top3_net_cost4": float(np.mean(drop) - COST_POINTS) if len(drop) else np.nan,
        "exit_recovery_rate": float(np.mean(reasons == "liquidity_recovery")),
        "exit_timeout_rate": float(np.mean(reasons == "timeout")),
        "exit_stop_rate": float(np.mean(reasons == "stop")),
    }


def evaluate_exit_grid(entries: pd.DataFrame, windows: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    post = windows[windows["dt"].between(0, 900)].copy()
    dt_cols = np.array(sorted(post["dt"].unique()), dtype=int)

    def pivot(col: str) -> pd.DataFrame:
        return (
            post.pivot_table(index="event_id", columns="dt", values=col, aggfunc="last")
            .reindex(columns=dt_cols)
            .sort_index()
        )

    ret_p = pivot("ret_common")
    d5_p = pivot("D5")
    z_p = pivot("zLogL")
    spread_p = pivot("spread")

    work = entries.copy().reset_index(drop=True)
    event_order = ret_p.index
    ret = ret_p.reindex(work["event_id"]).to_numpy(dtype=float)
    d5 = d5_p.reindex(work["event_id"]).to_numpy(dtype=float)
    z = z_p.reindex(work["event_id"]).to_numpy(dtype=float)
    spr = spread_p.reindex(work["event_id"]).to_numpy(dtype=float)
    direction = work["direction"].to_numpy(dtype=float)
    pnl_mat = direction[:, None] * ret
    d5_ratio_mat = d5 / work["D5_t0"].replace(0, np.nan).to_numpy(dtype=float)[:, None]
    z_delta_mat = z - work["zLogL_t0"].to_numpy(dtype=float)[:, None]
    spread_ratio_mat = spr / work["spread_t0"].replace(0, np.nan).to_numpy(dtype=float)[:, None]

    grid = []
    for mode in ["d5_z", "d5_spread", "z_spread", "all3", "score2of3"]:
        for min_hold in [30, 60, 120, 180]:
            for max_hold in [300, 600, 900]:
                for d5_ratio in [1.0, 1.1, 1.25, 1.5]:
                    for z_delta in [0.0, 0.5, 1.0, 1.5]:
                        for spread_ratio in [1.0, 0.9, 0.8]:
                            grid.append((mode, min_hold, max_hold, d5_ratio, z_delta, spread_ratio))

    summaries = []
    trades = []

    for mode, min_hold, max_hold, d5_th, z_th, spr_th in grid:
        exit_name = f"{mode}_min{min_hold}_max{max_hold}_d5{d5_th}_z{z_th}_spr{spr_th}"
        valid_time = (dt_cols >= min_hold) & (dt_cols <= max_hold)
        if not valid_time.any():
            continue
        c1 = d5_ratio_mat >= d5_th
        c2 = z_delta_mat >= z_th
        c3 = spread_ratio_mat <= spr_th
        if mode == "d5_z":
            trigger = c1 & c2
        elif mode == "d5_spread":
            trigger = c1 & c3
        elif mode == "z_spread":
            trigger = c2 & c3
        elif mode == "all3":
            trigger = c1 & c2 & c3
        else:
            trigger = (c1.astype(np.int8) + c2.astype(np.int8) + c3.astype(np.int8)) >= 2
        trigger &= valid_time[None, :]
        stop = (pnl_mat <= -12.0) & valid_time[None, :]
        hit = trigger | stop
        any_hit = hit.any(axis=1)
        first_idx = hit.argmax(axis=1)
        timeout_idx = np.flatnonzero(valid_time)[-1]
        exit_idx = np.where(any_hit, first_idx, timeout_idx)
        row_idx = np.arange(len(work))
        exit_dt = dt_cols[exit_idx]
        pnl = pnl_mat[row_idx, exit_idx]
        reasons = np.where(any_hit, np.where(stop[row_idx, exit_idx], "stop", "liquidity_recovery"), "timeout")
        tdf = work[["event_id", "entry_rule", "split", "day"]].copy()
        tdf["exit_rule"] = exit_name
        tdf["exit_dt"] = exit_dt
        tdf["pnl"] = pnl
        tdf["reason"] = reasons
        for (rule, split), g in tdf.groupby(["entry_rule", "split"], sort=True):
            e = work[(work["entry_rule"].eq(rule)) & (work["split"].eq(split))]
            summaries.append(
                summarize(
                    rule,
                    split,
                    exit_name,
                    e,
                    g["pnl"].to_numpy(dtype=float),
                    g["exit_dt"].to_numpy(dtype=float),
                    g["reason"].to_numpy(dtype=object),
                )
            )
        if len(trades) < 20:
            trades.append(tdf)
    return pd.DataFrame(summaries), pd.concat(trades, ignore_index=True) if trades else pd.DataFrame()


def write_html(tables: dict[str, pd.DataFrame], meta: dict[str, object]) -> None:
    css = "body{font-family:Arial;margin:24px;color:#17202a} table{border-collapse:collapse;font-size:12px;margin:14px 0} th,td{border:1px solid #ddd;padding:4px 6px;text-align:right} th{text-align:left;background:#eef2f6}.note{max-width:1120px;line-height:1.5}"
    body = [
        "<!doctype html><html><head><meta charset='utf-8'><title>PDQ Liquidity Recovery Exit</title>",
        f"<style>{css}</style></head><body><h1>PDQ Liquidity Recovery Exit Audit</h1>",
        "<p class='note'>Entry uses t0-available PDQ + Supertrend/TXF/VWAP gates. Exit triggers when D5/zLogL/spread indicate book-thickness or liquidity recovery after a minimum hold. PnL is common-mid path proxy in TAIFEX points.</p>",
        f"<pre>{html.escape(json.dumps(meta, indent=2, sort_keys=True))}</pre>",
    ]
    for name, df in tables.items():
        body.append(f"<h2>{html.escape(name)}</h2>")
        body.append(df.round(4).to_html(index=False, escape=True))
    body.append("</body></html>")
    (OUT_DIR / "index.html").write_text("\n".join(body))


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    events = pd.read_csv(EVENTS_PATH)
    entries = build_entries(events)
    event_ids = set(entries["event_id"].astype(str))
    windows = load_windows(event_ids)
    summary, sample_trades = evaluate_exit_grid(entries, windows)
    fixed = pd.concat([fixed_summary(entries, h) for h in [300, 600, 900]], ignore_index=True)

    entries.to_csv(OUT_DIR / "entry_events.csv", index=False)
    summary.to_csv(OUT_DIR / "liquidity_recovery_exit_grid.csv", index=False)
    fixed.to_csv(OUT_DIR / "fixed_hold_baseline.csv", index=False)
    sample_trades.to_csv(OUT_DIR / "sample_exit_trades.csv", index=False)

    robust = summary[
        (summary["split"].eq("OOS"))
        & (summary["n"] >= 80)
        & (summary["active_days"] >= 15)
    ].sort_values(["drop_top3_net_cost4", "net_cost4"], ascending=False)
    top_fixed = fixed[fixed["split"].eq("OOS")].sort_values(["drop_top3_net_cost4", "net_cost4"], ascending=False)
    meta = {
        "source_events": str(EVENTS_PATH.relative_to(ROOT)),
        "source_windows": str(WINDOWS_PATH.relative_to(ROOT)),
        "entry_count": int(len(entries)),
        "unique_event_count": int(entries["event_id"].nunique()),
        "exit_grid_rows": int(len(summary)),
        "cost_points": COST_POINTS,
        "entry_note": "No post-entry liquidity_state_30 used as entry gate.",
        "exit_note": "Liquidity recovery is evaluated after entry using D5 ratio, zLogL delta, and spread ratio.",
        "stop_points": -12.0,
    }
    (OUT_DIR / "metadata.json").write_text(json.dumps(meta, indent=2, sort_keys=True))
    write_html(
        {
            "Top OOS Liquidity-Recovery Exits": robust.head(80),
            "OOS Fixed-Hold Baseline": top_fixed,
            "All Entry Counts": entries.groupby(["entry_rule", "split"]).size().reset_index(name="n"),
        },
        meta,
    )


if __name__ == "__main__":
    main()
