"""Full PDQ_cont routing audit requested after the visual atlas.

This extends the wrong-way decomposition with the missing checks:
- VWAP-style proxy direction
- explicit external-market availability audit
- taker TP/SL grid for real-time proxy rules
- clean/chop/toxicity score deciles
- touch-fill passive maker proxy, including neutral and skewed variants

The maker section is intentionally labelled as a touch-fill proxy.  The atlas
does not contain trade prints, queue position, or order-level fills.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
ATLAS_DIR = ROOT / "outputs/liquidity_score/pdq_visual_atlas"
WRONG_DIR = ROOT / "outputs/liquidity_score/pdq_wrongway_decomposition"
OUT_DIR = ROOT / "outputs/liquidity_score/pdq_full_routing_audit"


def signed(values: pd.Series | np.ndarray) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    out = np.zeros(arr.shape, dtype=np.int8)
    out[arr > 0] = 1
    out[arr < 0] = -1
    return out


def load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    events = pd.read_csv(WRONG_DIR / "event_wrongway_features.csv")
    windows = pd.read_csv(
        ATLAS_DIR / "event_windows.csv.gz",
        usecols=[
            "event_id",
            "dt",
            "mid_common",
            "ret_common",
            "spread",
            "D5",
            "zLogL",
            "C60",
            "e_TXF",
            "RVExp",
            "CrossSync",
        ],
    )
    return events, windows


def add_vwap_proxy(events: pd.DataFrame, windows: pd.DataFrame) -> pd.DataFrame:
    pre = windows[(windows["dt"] >= -300) & (windows["dt"] <= 0)].copy()
    vwap = (
        pre.groupby("event_id", sort=False)
        .agg(
            vwap300_proxy=("mid_common", "mean"),
            pre_mid_first=("mid_common", "first"),
            pre_mid_last=("mid_common", "last"),
        )
        .reset_index()
    )
    out = events.merge(vwap, on="event_id", how="left", validate="one_to_one")
    out["VWAP300_bias"] = signed(out["pre_mid_last"] - out["vwap300_proxy"])
    out["VWAP300_C_state"] = np.select(
        [
            (out["VWAP300_bias"] != 0) & (out["VWAP300_bias"] == out["entry_dir_C"]),
            (out["VWAP300_bias"] != 0) & (out["VWAP300_bias"] == -out["entry_dir_C"]),
        ],
        ["VWAP_C_aligned", "VWAP_C_opposite"],
        default="VWAP_zero",
    )
    return out


def first_passage_for_dir(events: pd.DataFrame, direction: pd.Series | np.ndarray, tp: float, sl: float) -> pd.DataFrame:
    d = np.asarray(direction, dtype=float)
    t_up_tp = events[f"t_up{int(tp)}"] if f"t_up{int(tp)}" in events else events["t_up8"]
    t_dn_tp = events[f"t_down{int(tp)}"] if f"t_down{int(tp)}" in events else events["t_down8"]
    # Atlas stores only 8pt touch times.  For larger TP/SL, use MFE/MAE/final
    # path feasibility as a coarse barrier proxy from max/min, not exact timing.
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
    # Timing is unknown for non-8 barriers; mark both-hit as ambiguous timeout.
    tp_first = tp_hit & ~sl_hit
    sl_first = sl_hit & ~tp_hit
    timeout = ~(tp_first | sl_first)
    return pd.DataFrame({"tp_first": tp_first, "sl_first": sl_first, "timeout": timeout})


def summarize_barrier(events: pd.DataFrame, direction_col: str, tp: float, sl: float) -> dict[str, float | int]:
    if events.empty:
        return {"n": 0, "active_days": 0}
    d = events[direction_col].fillna(0).astype(float)
    valid = d != 0
    if valid.sum() == 0:
        return {"n": 0, "active_days": 0}
    sub = events.loc[valid]
    fp = first_passage_for_dir(sub, d.loc[valid], tp, sl)
    final_dir = d.loc[valid].to_numpy() * sub["final_ret"].to_numpy()
    return {
        "n": int(valid.sum()),
        "active_days": int(sub["day"].nunique()),
        "tp_first": float(fp["tp_first"].mean()),
        "sl_first": float(fp["sl_first"].mean()),
        "timeout": float(fp["timeout"].mean()),
        "fp_edge": float(fp["tp_first"].mean() - fp["sl_first"].mean()),
        "final_dir_mean": float(np.nanmean(final_dir)),
        "final_dir_median": float(np.nanmedian(final_dir)),
        "clean_share": float(sub["trade_path_label_C"].eq("clean_continuation").mean()),
        "wrong_way_share": float(sub["trade_path_label_C"].eq("wrong_way_first").mean()),
        "two_sided_share": float(sub["trade_path_label_C"].eq("two_sided_chop").mean()),
    }


def taker_grid(events: pd.DataFrame) -> pd.DataFrame:
    rules = [
        ("all_signC", "entry_dir_C", "event_id == event_id"),
        ("rt_clean_taker_proxy_t0", "entry_dir_C", "clean_taker_proxy_t0"),
        ("rt_reverse_proxy_t0", "reverse_C_dir", "reverse_proxy_t0"),
        ("rt_reverse_proxy_post30", "reverse_C_dir", "reverse_proxy_post30"),
        ("VWAP_C_opposite_reverse", "VWAP300_bias", "VWAP300_C_state == 'VWAP_C_opposite'"),
        ("TSI_C_opposite_TSI", "TSI15_dir", "TSI_C_state == 'opposite'"),
        ("OR_C_opposite_OR", "OR_bias", "OR_C_state == 'OR_C_opposite'"),
        ("eTXF_C_opposite_eTXF", "e_TXF_dir", "TXF_C_state == 'e_TXF_C_opposite'"),
    ]
    grid = [(8, 8, 300), (12, 8, 300), (16, 8, 600), (24, 12, 600)]
    rows = []
    for rule, dir_col, query in rules:
        base = events.query(query).copy()
        for split, group in base.groupby("split", sort=True):
            for tp, sl, maxhold in grid:
                row = {
                    "rule": rule,
                    "split": split,
                    "direction": dir_col,
                    "tp": tp,
                    "sl": sl,
                    "maxhold_s": maxhold,
                    "barrier_note": "exact timing only for 8/8; larger barriers use max/min feasibility proxy",
                }
                row.update(summarize_barrier(group, dir_col, tp, sl))
                rows.append(row)
    return pd.DataFrame(rows)


def score_deciles(events: pd.DataFrame) -> pd.DataFrame:
    out = events.copy()
    out["clean_score_proxy"] = (
        (out["TXF_C_state"].eq("e_TXF_C_aligned")).astype(float)
        + out["CPersist_gt_q50"].astype(float)
        + (out["CrossSync_t0"] >= 3).astype(float)
        - out["Toxicity_q70"].astype(float)
    )
    out["chop_score_proxy"] = (
        (~out["CSpike_q80"]).astype(float)
        + (~out["CPersist_gt_q50"]).astype(float)
        + (out["spread_t0"] > out.groupby("split")["spread_t0"].transform("median")).astype(float)
        + (out["D5_t0"] > out.groupby("split")["D5_t0"].transform("median")).astype(float)
        - out["Toxicity_q70"].astype(float)
    )
    out["toxicity_score_proxy"] = (
        out["Toxicity_q70"].astype(float)
        + out["liquidity_state_30"].eq("deteriorating").astype(float)
        + (out["zLogL_t0"] < out.groupby("split")["zLogL_t0"].transform("median")).astype(float)
    )
    rows = []
    for score in ["clean_score_proxy", "chop_score_proxy", "toxicity_score_proxy"]:
        for split, sub in out.groupby("split", sort=True):
            ranked = sub.copy()
            ranked["decile"] = pd.qcut(ranked[score].rank(method="first"), 10, labels=False) + 1
            for decile, g in ranked.groupby("decile", sort=True):
                rows.append(
                    {
                        "score": score,
                        "split": split,
                        "decile": int(decile),
                        "n": len(g),
                        "clean_share": g["trade_path_label_C"].eq("clean_continuation").mean(),
                        "wrong_way_share": g["trade_path_label_C"].eq("wrong_way_first").mean(),
                        "two_sided_share": g["trade_path_label_C"].eq("two_sided_chop").mean(),
                        "toxicity_q70_share": g["Toxicity_q70"].mean(),
                        "final_signed_C_mean": g["final_signed_C"].mean(),
                        "mfe_C_median": g["mfe_C"].median(),
                        "mae_C_median": g["mae_C"].median(),
                    }
                )
    return pd.DataFrame(rows)


def passive_touch_fill(events: pd.DataFrame, windows: pd.DataFrame) -> pd.DataFrame:
    # Uses common mid and aggregate spread at t0.  Fill rule is touch-based:
    # bid fills if common mid falls to bid quote; ask fills if common mid rises to ask quote.
    ev = events[["event_id", "split", "day", "trade_path_label_C", "maker_rt_proxy_t0", "entry_dir_C", "TSI15_dir", "spread_t0"]].copy()
    post = windows[windows["dt"].between(0, 120)].copy()
    post = post.merge(ev[["event_id", "spread_t0", "entry_dir_C", "TSI15_dir", "maker_rt_proxy_t0", "trade_path_label_C", "split", "day"]], on="event_id", how="inner")
    post["half_spread"] = post["spread_t0"] / 2.0
    horizons = [5, 30, 60]
    rows = []
    for event_id, w in post.groupby("event_id", sort=False):
        meta = w.iloc[0]
        bid_fill_rows = w[w["ret_common"] <= -meta["half_spread"]]
        ask_fill_rows = w[w["ret_common"] >= meta["half_spread"]]
        for side, fills in [("bid_buy", bid_fill_rows), ("ask_sell", ask_fill_rows)]:
            if fills.empty:
                fill_dt = np.nan
                fill_price_offset = np.nan
            else:
                fill_dt = float(fills.iloc[0]["dt"])
                fill_price_offset = -meta["half_spread"] if side == "bid_buy" else meta["half_spread"]
            row = {
                "event_id": event_id,
                "split": meta["split"],
                "day": meta["day"],
                "trade_path_label_C": meta["trade_path_label_C"],
                "maker_rt_proxy_t0": bool(meta["maker_rt_proxy_t0"]),
                "side": side,
                "filled": bool(np.isfinite(fill_dt)),
                "fill_dt": fill_dt,
                "spread_t0": float(meta["spread_t0"]),
            }
            for h in horizons:
                hrow = w[w["dt"] >= h].head(1)
                if hrow.empty or not np.isfinite(fill_dt):
                    row[f"postfill_pnl_{h}s"] = np.nan
                else:
                    ret_h = float(hrow.iloc[0]["ret_common"])
                    if side == "bid_buy":
                        row[f"postfill_pnl_{h}s"] = ret_h - fill_price_offset
                    else:
                        row[f"postfill_pnl_{h}s"] = fill_price_offset - ret_h
            rows.append(row)
    return pd.DataFrame(rows)


def passive_summary(fills: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (split, label, maker, side), g in fills.groupby(["split", "trade_path_label_C", "maker_rt_proxy_t0", "side"], sort=True):
        row = {
            "split": split,
            "trade_path_label_C": label,
            "maker_rt_proxy_t0": bool(maker),
            "side": side,
            "n_quotes": len(g),
            "fill_rate": g["filled"].mean(),
            "avg_fill_dt": g.loc[g["filled"], "fill_dt"].mean(),
            "spread_t0_median": g["spread_t0"].median(),
        }
        for h in [5, 30, 60]:
            pnl = g[f"postfill_pnl_{h}s"]
            row[f"postfill_pnl_{h}s_mean"] = pnl.mean()
            row[f"postfill_pnl_{h}s_median"] = pnl.median()
            row[f"postfill_pnl_{h}s_hit"] = (pnl > 0).mean()
        rows.append(row)
    return pd.DataFrame(rows)


def external_market_audit() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "requested_item": "external market direction confirmation",
                "status": "not_tested_missing_data",
                "reason": "atlas/exported secbar contains TXF/MXF/TMF only; no NK/ES/FX/TWSE external market series available in artifact",
                "required_next_data": "timestamp-aligned external futures/index/FX returns at <=1s or completed-bar cadence",
            }
        ]
    )


def write_html(tables: dict[str, pd.DataFrame], meta: dict[str, object]) -> None:
    css = "body{font-family:Arial;margin:24px;color:#17202a} table{border-collapse:collapse;font-size:12px;margin:14px 0} th,td{border:1px solid #ddd;padding:4px 6px;text-align:right} th{text-align:left;background:#eef2f6}.note{max-width:1100px;line-height:1.5}"
    body = ["<!doctype html><html><head><meta charset='utf-8'><title>PDQ Full Routing Audit</title>", f"<style>{css}</style></head><body>", "<h1>PDQ Full Routing Audit</h1>"]
    body.append("<p class='note'>This fills the missing requested tests: VWAP proxy, external-market audit, taker TP/SL grid, score deciles, and passive touch-fill maker proxy. Larger TP/SL barriers are feasibility proxies unless TP=SL=8 because the atlas stores exact first-passage times only for +/-8.</p>")
    body.append(f"<pre>{json.dumps(meta, indent=2, sort_keys=True)}</pre>")
    for name, df in tables.items():
        body.append(f"<h2>{name}</h2>")
        body.append(df.round(4).to_html(index=False, escape=True))
    body.append("</body></html>")
    (OUT_DIR / "index.html").write_text("\n".join(body))


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    events, windows = load_data()
    events = add_vwap_proxy(events, windows)
    tg = taker_grid(events)
    sd = score_deciles(events)
    fills = passive_touch_fill(events, windows)
    ps = passive_summary(fills)
    ext = external_market_audit()

    events.to_csv(OUT_DIR / "events_with_vwap_proxy.csv", index=False)
    tg.to_csv(OUT_DIR / "taker_tp_sl_grid.csv", index=False)
    sd.to_csv(OUT_DIR / "score_decile_audit.csv", index=False)
    fills.to_csv(OUT_DIR / "passive_touch_fill_events.csv", index=False)
    ps.to_csv(OUT_DIR / "passive_touch_fill_summary.csv", index=False)
    ext.to_csv(OUT_DIR / "external_market_audit.csv", index=False)

    meta = {
        "source_atlas": str(ATLAS_DIR.relative_to(ROOT)),
        "source_wrongway": str(WRONG_DIR.relative_to(ROOT)),
        "events": int(len(events)),
        "maker_fill_model": "touch-fill proxy using common mid and aggregate spread, not queue/trade-print fill model",
        "vwap_proxy": "local t-300s..t0 mean common mid; not official volume VWAP",
        "external_market": "not available in current atlas artifacts",
        "price_unit": "TAIFEX points",
    }
    (OUT_DIR / "metadata.json").write_text(json.dumps(meta, indent=2, sort_keys=True))
    write_html(
        {
            "Taker TP/SL Grid": tg,
            "Score Decile Audit": sd,
            "Passive Touch-Fill Summary": ps,
            "External Market Audit": ext,
        },
        meta,
    )


if __name__ == "__main__":
    main()
