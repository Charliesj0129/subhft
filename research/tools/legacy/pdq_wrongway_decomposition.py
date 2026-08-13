"""Decompose PDQ_cont wrong-way events into reversal/toxic/chop candidates.

This tool consumes the canonical PDQ_cont atlas artifacts.  It does not create
new PDQ events and it does not use future path labels as rule inputs.  It tests
whether wrong-way-first events can be separated by low-frequency conflict,
C spike/persistence, and liquidity recovery.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
ATLAS_DIR = ROOT / "outputs/liquidity_score/pdq_visual_atlas"
OUT_DIR = ROOT / "outputs/liquidity_score/pdq_wrongway_decomposition"
UP = 8.0
DOWN = -8.0


def signed(arr: pd.Series | np.ndarray) -> np.ndarray:
    x = np.asarray(arr, dtype=float)
    out = np.zeros(x.shape, dtype=np.int8)
    out[x > 0] = 1
    out[x < 0] = -1
    return out


def direction_first_passage(events: pd.DataFrame, direction: pd.Series | np.ndarray) -> pd.DataFrame:
    d = np.asarray(direction, dtype=float)
    t_up = events["t_up8"].to_numpy(dtype=float)
    t_down = events["t_down8"].to_numpy(dtype=float)
    tp_time = np.where(d > 0, t_up, np.where(d < 0, t_down, np.nan))
    sl_time = np.where(d > 0, t_down, np.where(d < 0, t_up, np.nan))
    tp_first = np.isfinite(tp_time) & (~np.isfinite(sl_time) | (tp_time < sl_time))
    sl_first = np.isfinite(sl_time) & (~np.isfinite(tp_time) | (sl_time < tp_time))
    both_hit = np.isfinite(tp_time) & np.isfinite(sl_time)
    no_hit = ~np.isfinite(tp_time) & ~np.isfinite(sl_time)
    return pd.DataFrame(
        {
            "tp_time": tp_time,
            "sl_time": sl_time,
            "tp_first": tp_first,
            "sl_first": sl_first,
            "both_hit": both_hit,
            "no_hit": no_hit,
            "fp_edge": tp_first.astype(float) - sl_first.astype(float),
        },
        index=events.index,
    )


def summarize_group(events: pd.DataFrame, direction: pd.Series | np.ndarray) -> dict[str, float | int]:
    if len(events) == 0:
        return {
            "n": 0,
            "active_days": 0,
            "tp_first": np.nan,
            "sl_first": np.nan,
            "fp_edge": np.nan,
            "both_hit": np.nan,
            "no_hit": np.nan,
            "mfe_dir_median": np.nan,
            "mae_dir_median": np.nan,
            "final_dir_mean": np.nan,
            "two_sided_rate": np.nan,
        }
    fp = direction_first_passage(events, direction)
    d = np.asarray(direction, dtype=float)
    final_dir = d * events["final_ret"].to_numpy(dtype=float)
    mfe_dir = np.where(d > 0, events["max_ret"], -events["min_ret"]).astype(float)
    mae_dir = np.where(d > 0, events["min_ret"], -events["max_ret"]).astype(float)
    valid = d != 0
    if valid.sum() == 0:
        return {
            "n": int(len(events)),
            "active_days": int(events["day"].nunique()),
            "tp_first": np.nan,
            "sl_first": np.nan,
            "fp_edge": np.nan,
            "both_hit": np.nan,
            "no_hit": np.nan,
            "mfe_dir_median": np.nan,
            "mae_dir_median": np.nan,
            "final_dir_mean": np.nan,
            "two_sided_rate": float(events["two_sided"].mean()),
        }
    return {
        "n": int(valid.sum()),
        "active_days": int(events.loc[valid, "day"].nunique()),
        "tp_first": float(fp.loc[valid, "tp_first"].mean()),
        "sl_first": float(fp.loc[valid, "sl_first"].mean()),
        "fp_edge": float(fp.loc[valid, "fp_edge"].mean()),
        "both_hit": float(fp.loc[valid, "both_hit"].mean()),
        "no_hit": float(fp.loc[valid, "no_hit"].mean()),
        "mfe_dir_median": float(np.nanmedian(mfe_dir[valid])),
        "mae_dir_median": float(np.nanmedian(mae_dir[valid])),
        "final_dir_mean": float(np.nanmean(final_dir[valid])),
        "two_sided_rate": float(events.loc[valid, "two_sided"].mean()),
    }


def load_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    events = pd.read_csv(ATLAS_DIR / "event_labels.csv")
    usecols = [
        "event_id",
        "dt",
        "C60",
        "e_TXF",
        "e_TMF",
        "RVExp",
        "CrossSync",
        "zLogL",
        "spread",
        "D5",
    ]
    windows = pd.read_csv(ATLAS_DIR / "event_windows.csv.gz", usecols=usecols)
    return events, windows


def window_features(events: pd.DataFrame, windows: pd.DataFrame) -> pd.DataFrame:
    pre = windows[(windows["dt"] >= -300) & (windows["dt"] <= -60)].copy()
    pre["abs_C60"] = pre["C60"].abs()
    pre_agg = (
        pre.groupby("event_id", sort=False)
        .agg(
            pre_abs_C60_median=("abs_C60", "median"),
            pre_C60_mean=("C60", "mean"),
            pre_RVExp_median=("RVExp", "median"),
            pre_CrossSync_mean=("CrossSync", "mean"),
        )
        .reset_index()
    )
    near = windows[(windows["dt"] >= -60) & (windows["dt"] <= 0)].copy()
    sign_lookup = events.set_index("event_id")["entry_dir_C"]
    near["entry_dir_C"] = near["event_id"].map(sign_lookup).fillna(0).astype(np.int8)
    near["C_same_sign"] = (signed(near["C60"]) == near["entry_dir_C"].to_numpy()).astype(float)
    near_agg = (
        near.groupby("event_id", sort=False)
        .agg(
            C_persistence_60=("C_same_sign", "mean"),
            C60_near_mean=("C60", "mean"),
            C60_near_abs_mean=("C60", lambda s: float(np.nanmean(np.abs(s)))),
            CrossSync_near_mean=("CrossSync", "mean"),
            RVExp_near_mean=("RVExp", "mean"),
        )
        .reset_index()
    )
    t0 = (
        windows[windows["dt"].eq(0)]
        .groupby("event_id", sort=False)
        .agg(
            zLogL_0=("zLogL", "last"),
            spread_0=("spread", "last"),
            D5_0=("D5", "last"),
            C60_0=("C60", "last"),
            e_TXF_0=("e_TXF", "last"),
            e_TMF_0=("e_TMF", "last"),
        )
        .reset_index()
    )
    t30 = (
        windows[windows["dt"].between(25, 35)]
        .sort_values(["event_id", "dt"])
        .groupby("event_id", sort=False)
        .agg(
            zLogL_30=("zLogL", "last"),
            spread_30=("spread", "last"),
            D5_30=("D5", "last"),
            C60_30=("C60", "last"),
            e_TXF_30=("e_TXF", "last"),
            e_TMF_30=("e_TMF", "last"),
        )
        .reset_index()
    )
    t60 = (
        windows[windows["dt"].between(0, 60)]
        .groupby("event_id", sort=False)
        .agg(
            zLogL_post60_mean=("zLogL", "mean"),
            spread_post60_mean=("spread", "mean"),
            D5_post60_mean=("D5", "mean"),
            C60_post60_mean=("C60", "mean"),
            CrossSync_post60_mean=("CrossSync", "mean"),
        )
        .reset_index()
    )
    features = events[["event_id"]].merge(pre_agg, on="event_id", how="left")
    for add in (near_agg, t0, t30, t60):
        features = features.merge(add, on="event_id", how="left")
    features["CSpike"] = events["abs_C60"].to_numpy(dtype=float) / (
        features["pre_abs_C60_median"].to_numpy(dtype=float) + 1e-9
    )
    features["zLogL_delta30"] = features["zLogL_30"] - features["zLogL_0"]
    features["spread_delta30"] = features["spread_30"] - features["spread_0"]
    features["D5_delta30"] = features["D5_30"] - features["D5_0"]
    features["liquidity_recovery_score"] = (
        (features["zLogL_delta30"] > 0).astype(int)
        + (features["spread_delta30"] < 0).astype(int)
        + (features["D5_delta30"] > 0).astype(int)
    )
    features["liquidity_deterioration_score"] = (
        (features["zLogL_delta30"] < 0).astype(int)
        + (features["spread_delta30"] > 0).astype(int)
        + (features["D5_delta30"] < 0).astype(int)
    )
    return features


def add_realtime_features(events: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
    df = events.merge(features, on="event_id", how="left", validate="one_to_one")
    df["reverse_C_dir"] = -df["entry_dir_C"].astype(np.int8)
    df["e_TXF_dir"] = signed(df["e_TXF_t0"])
    df["e_TMF_dir"] = signed(df["e_TMF_t0"])
    df["TXF_C_state"] = np.select(
        [
            (df["e_TXF_dir"] != 0) & (df["e_TXF_dir"] == df["entry_dir_C"]),
            (df["e_TXF_dir"] != 0) & (df["e_TXF_dir"] == -df["entry_dir_C"]),
        ],
        ["e_TXF_C_aligned", "e_TXF_C_opposite"],
        default="e_TXF_zero",
    )
    df["OR_C_state"] = np.select(
        [
            (df["OR_bias"] != 0) & (df["OR_bias"] == df["entry_dir_C"]),
            (df["OR_bias"] != 0) & (df["OR_bias"] == -df["entry_dir_C"]),
        ],
        ["OR_C_aligned", "OR_C_opposite"],
        default="OR_zero",
    )
    df["TSI_strength_proxy"] = df["TSI15_dir"].abs()
    df["zLogL_recovering"] = df["zLogL_delta30"] > 0
    df["spread_recovering"] = df["spread_delta30"] < 0
    df["D5_recovering"] = df["D5_delta30"] > 0
    df["liquidity_state_30"] = np.select(
        [df["liquidity_recovery_score"] >= 2, df["liquidity_deterioration_score"] >= 2],
        ["recovering", "deteriorating"],
        default="mixed",
    )
    return df


def split_quantile_flags(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["CSpike_q80"] = False
    out["CPersist_gt_q50"] = False
    out["Toxicity_q70"] = False
    for split, sub in out.groupby("split", sort=False):
        idx = sub.index
        out.loc[idx, "CSpike_q80"] = sub["CSpike"] >= sub["CSpike"].quantile(0.80)
        out.loc[idx, "CPersist_gt_q50"] = sub["C_persistence_60"] >= sub[
            "C_persistence_60"
        ].quantile(0.50)
        tox = (
            (-sub["zLogL_t0"].fillna(0))
            + sub["spread_t0"].rank(pct=True)
            - sub["D5_t0"].rank(pct=True)
        )
        out.loc[idx, "Toxicity_q70"] = tox >= tox.quantile(0.70)
    out["C_shape_bucket"] = np.select(
        [
            out["CSpike_q80"] & ~out["CPersist_gt_q50"],
            out["CSpike_q80"] & out["CPersist_gt_q50"],
            ~out["CSpike_q80"] & out["CPersist_gt_q50"],
        ],
        ["high_spike_low_persist", "high_spike_high_persist", "low_spike_high_persist"],
        default="low_spike_low_persist",
    )
    out["wrongway_reversal_candidate"] = (
        out["trade_path_label_C"].eq("wrong_way_first")
        & out["TSI_C_state"].eq("opposite")
        & out["C_shape_bucket"].eq("high_spike_low_persist")
        & out["liquidity_state_30"].eq("recovering")
        & ~out["Toxicity_q70"]
    )
    out["wrongway_toxic_candidate"] = (
        out["trade_path_label_C"].eq("wrong_way_first")
        & (
            out["TXF_C_state"].eq("e_TXF_C_opposite")
            | out["liquidity_state_30"].eq("deteriorating")
            | out["Toxicity_q70"]
        )
    )
    out["maker_proxy_candidate"] = (
        out["trade_path_label_C"].eq("two_sided_chop")
        & out["spread_t0"].gt(out.groupby("split")["spread_t0"].transform("median"))
        & out["D5_t0"].gt(out.groupby("split")["D5_t0"].transform("median"))
        & ~out["Toxicity_q70"]
    )
    out["reverse_proxy_t0"] = (
        out["TSI_C_state"].eq("opposite")
        & out["C_shape_bucket"].eq("high_spike_low_persist")
        & ~out["Toxicity_q70"]
    )
    out["reverse_proxy_post30"] = out["reverse_proxy_t0"] & out["liquidity_state_30"].eq(
        "recovering"
    )
    out["clean_taker_proxy_t0"] = (
        out["TXF_C_state"].eq("e_TXF_C_aligned")
        & out["CPersist_gt_q50"]
        & out["CrossSync_t0"].ge(3)
        & ~out["Toxicity_q70"]
    )
    out["maker_rt_proxy_t0"] = (
        out["spread_t0"].gt(out.groupby("split")["spread_t0"].transform("median"))
        & out["D5_t0"].gt(out.groupby("split")["D5_t0"].transform("median"))
        & ~out["Toxicity_q70"]
        & ~out["CSpike_q80"]
        & ~out["CPersist_gt_q50"]
    )
    return out


def group_summary(
    df: pd.DataFrame,
    group_col: str,
    direction_col: str,
    subset_query: str | None = None,
) -> pd.DataFrame:
    data = df.query(subset_query).copy() if subset_query else df.copy()
    rows = []
    for (split, group), sub in data.groupby(["split", group_col], dropna=False, sort=True):
        row = {"split": split, group_col: group, "direction": direction_col}
        row.update(summarize_group(sub, sub[direction_col]))
        rows.append(row)
    return pd.DataFrame(rows)


def test1_lowfreq(wrong: pd.DataFrame) -> pd.DataFrame:
    frames = []
    for group_col, direction_col in [
        ("TSI_C_state", "TSI15_dir"),
        ("OR_C_state", "OR_bias"),
        ("TXF_C_state", "e_TXF_dir"),
        ("TSI_C_state", "reverse_C_dir"),
    ]:
        s = group_summary(wrong, group_col, direction_col)
        s.insert(0, "test", "T1_wrongway_direction_anchor")
        frames.append(s)
    return pd.concat(frames, ignore_index=True)


def test2_spike_ramp(wrong: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for direction_col in ("reverse_C_dir", "TSI15_dir", "e_TXF_dir"):
        for (split, bucket), sub in wrong.groupby(["split", "C_shape_bucket"], sort=True):
            row = {
                "test": "T2_wrongway_spike_vs_ramp",
                "split": split,
                "C_shape_bucket": bucket,
                "direction": direction_col,
                "CSpike_median": float(sub["CSpike"].median()),
                "CPersistence_median": float(sub["C_persistence_60"].median()),
            }
            row.update(summarize_group(sub, sub[direction_col]))
            rows.append(row)
    return pd.DataFrame(rows)


def test3_liquidity_recovery(wrong: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for direction_col in ("reverse_C_dir", "TSI15_dir", "e_TXF_dir"):
        for (split, state), sub in wrong.groupby(["split", "liquidity_state_30"], sort=True):
            row = {
                "test": "T3_wrongway_liquidity_recovery",
                "split": split,
                "liquidity_state_30": state,
                "direction": direction_col,
                "zLogL_delta30_median": float(sub["zLogL_delta30"].median()),
                "spread_delta30_median": float(sub["spread_delta30"].median()),
                "D5_delta30_median": float(sub["D5_delta30"].median()),
            }
            row.update(summarize_group(sub, sub[direction_col]))
            rows.append(row)
    return pd.DataFrame(rows)


def candidate_summary(df: pd.DataFrame) -> pd.DataFrame:
    specs = [
        ("oracle_clean_taker_label", "trade_path_label_C == 'clean_continuation'", "entry_dir_C", "oracle"),
        ("oracle_wrongway_reverse_label", "wrongway_reversal_candidate", "reverse_C_dir", "oracle"),
        ("oracle_wrongway_toxic_label", "wrongway_toxic_candidate", "reverse_C_dir", "oracle"),
        ("oracle_two_sided_maker_label", "maker_proxy_candidate", "entry_dir_C", "oracle"),
        ("oracle_two_sided_all", "trade_path_label_C == 'two_sided_chop'", "entry_dir_C", "oracle"),
        ("rt_clean_taker_proxy_t0", "clean_taker_proxy_t0", "entry_dir_C", "realtime_t0"),
        ("rt_reverse_proxy_t0", "reverse_proxy_t0", "reverse_C_dir", "realtime_t0"),
        ("rt_reverse_proxy_post30", "reverse_proxy_post30", "reverse_C_dir", "post30_confirmation"),
        ("rt_maker_proxy_t0", "maker_rt_proxy_t0", "entry_dir_C", "realtime_t0_proxy"),
    ]
    rows = []
    for name, query, direction_col, rule_type in specs:
        sub = df.query(query)
        for split, s in sub.groupby("split", sort=True):
            row = {
                "candidate": name,
                "rule_type": rule_type,
                "split": split,
                "direction": direction_col,
            }
            row.update(summarize_group(s, s[direction_col]))
            row["clean_share"] = float(s["trade_path_label_C"].eq("clean_continuation").mean())
            row["wrong_way_share"] = float(s["trade_path_label_C"].eq("wrong_way_first").mean())
            row["two_sided_share"] = float(s["trade_path_label_C"].eq("two_sided_chop").mean())
            row["toxicity_q70_share"] = float(s["Toxicity_q70"].mean())
            rows.append(row)
    return pd.DataFrame(rows)


def maker_proxy_summary(df: pd.DataFrame) -> pd.DataFrame:
    # This is not a fill model.  It only asks whether the path shape is plausibly
    # market-making friendly: two-sided, not toxic, enough spread/depth at t0.
    sub = df[df["trade_path_label_C"].isin(["two_sided_chop", "wrong_way_first"])].copy()
    rows = []
    for (split, label, maker), g in sub.groupby(
        ["split", "trade_path_label_C", "maker_proxy_candidate"], sort=True
    ):
        rows.append(
            {
                "split": split,
                "trade_path_label_C": label,
                "maker_proxy_candidate": bool(maker),
                "n": len(g),
                "active_days": g["day"].nunique(),
                "spread_t0_median": g["spread_t0"].median(),
                "D5_t0_median": g["D5_t0"].median(),
                "zLogL_t0_median": g["zLogL_t0"].median(),
                "mfe_C_median": g["mfe_C"].median(),
                "mae_C_median": g["mae_C"].median(),
                "final_signed_C_mean": g["final_signed_C"].mean(),
                "toxicity_q70_share": g["Toxicity_q70"].mean(),
                "liquidity_recovering_share": g["liquidity_state_30"].eq("recovering").mean(),
                "liquidity_deteriorating_share": g["liquidity_state_30"].eq("deteriorating").mean(),
            }
        )
    return pd.DataFrame(rows)


def write_html_index(tables: dict[str, pd.DataFrame], meta: dict[str, object]) -> None:
    css = """
    body{font-family:Arial,Helvetica,sans-serif;margin:24px;color:#17202a;background:#fbfcfd}
    table{border-collapse:collapse;margin:12px 0;font-size:12px} th,td{border:1px solid #d7dde5;padding:5px 7px;text-align:right}
    th{text-align:left;background:#eef2f6}.note{max-width:1050px;line-height:1.5}.warn{color:#9a3412}
    """
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'><title>PDQ Wrong-Way Decomposition</title>",
        f"<style>{css}</style></head><body><h1>PDQ Wrong-Way Decomposition</h1>",
        "<p class='note'>This report uses the fixed PDQ_cont atlas events. It separates wrong-way-first events by low-frequency conflict, C spike/persistence, and liquidity recovery. Maker results are proxy-only and are not fill simulation.</p>",
        f"<pre>{json.dumps(meta, indent=2, sort_keys=True)}</pre>",
    ]
    for name, df in tables.items():
        parts.append(f"<h2>{name}</h2>")
        parts.append(df.round(4).to_html(index=False, escape=True))
    parts.append("</body></html>")
    (OUT_DIR / "index.html").write_text("\n".join(parts))


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    events, windows = load_inputs()
    features = window_features(events, windows)
    enriched = add_realtime_features(events, features)
    enriched = split_quantile_flags(enriched)
    wrong = enriched[enriched["trade_path_label_C"].eq("wrong_way_first")].copy()

    test1 = test1_lowfreq(wrong)
    test2 = test2_spike_ramp(wrong)
    test3 = test3_liquidity_recovery(wrong)
    candidates = candidate_summary(enriched)
    maker_proxy = maker_proxy_summary(enriched)

    enriched.to_csv(OUT_DIR / "event_wrongway_features.csv", index=False)
    wrong.to_csv(OUT_DIR / "wrongway_events_enriched.csv", index=False)
    features.to_csv(OUT_DIR / "window_realtime_features.csv", index=False)
    test1.to_csv(OUT_DIR / "test1_wrongway_lowfreq_direction.csv", index=False)
    test2.to_csv(OUT_DIR / "test2_wrongway_spike_persistence.csv", index=False)
    test3.to_csv(OUT_DIR / "test3_wrongway_liquidity_recovery.csv", index=False)
    candidates.to_csv(OUT_DIR / "candidate_routing_summary.csv", index=False)
    maker_proxy.to_csv(OUT_DIR / "maker_proxy_suitability.csv", index=False)

    meta = {
        "source_atlas": str(ATLAS_DIR.relative_to(ROOT)),
        "events": int(len(enriched)),
        "wrong_way_first_events": int(len(wrong)),
        "price_unit": "TAIFEX points",
        "tests": [
            "T1 wrong-way low-frequency / residual direction anchors",
            "T2 C spike vs persistence buckets",
            "T3 liquidity recovery after wrong-way start",
            "Candidate routing summary for taker/reverse/no-trade/maker-proxy",
        ],
        "maker_caveat": "maker_proxy_suitability is path/toxicity proxy only; no passive fill simulation or queue model.",
    }
    (OUT_DIR / "metadata.json").write_text(json.dumps(meta, indent=2, sort_keys=True))
    write_html_index(
        {
            "T1 low-frequency direction": test1,
            "T2 spike/persistence": test2,
            "T3 liquidity recovery": test3,
            "Candidate routing summary": candidates,
            "Maker proxy suitability": maker_proxy,
        },
        meta,
    )


if __name__ == "__main__":
    main()
