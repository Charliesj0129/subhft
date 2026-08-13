"""Build a PDQ_cont event atlas from exported front-month L2 secbars.

The atlas fixes a canonical PDQ_cont event set as signal-run starts, extracts
event windows from t0-300s to t0+900s, assigns first-passage path labels, and
writes self-contained HTML visualizations without external plotting packages.
"""

from __future__ import annotations

import html
import importlib.util
import json
import math
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
import polars as pl


ROOT = Path(__file__).resolve().parents[2]
BASE_TOOL = ROOT / "research/tools/pdq_tsi15_decomposition_audit.py"
OUT_DIR = ROOT / "outputs/liquidity_score/pdq_visual_atlas"
PRE_S = 300
POST_S = 900
STEP_S = 5
UP_TH = 8.0
DOWN_TH = -8.0
ROOT_WEIGHTS = {"TXF": 0.5, "MXF": 0.3, "TMF": 0.2}
ROOTS = ("TXF", "MXF", "TMF")


def load_base_tool():
    spec = importlib.util.spec_from_file_location("pdq_base_tool", BASE_TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load base PDQ tool: {BASE_TOOL}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pdq = load_base_tool()


def ensure_dir() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)


def write_parquet(df: pd.DataFrame, path: Path) -> None:
    data = {}
    for col in df.columns:
        series = df[col]
        if series.dtype == object:
            data[col] = series.fillna("").astype(str).to_list()
        elif str(series.dtype).startswith("datetime"):
            data[col] = series.astype(str).to_list()
        else:
            data[col] = series.to_numpy()
    pl.DataFrame(data).write_parquet(path)


def write_parquet_from_csv(csv_path: Path, parquet_path: Path) -> None:
    pl.read_csv(csv_path).write_parquet(parquet_path)


def signed(values: pd.Series | np.ndarray, threshold: float = 0.0) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    out = np.zeros(arr.shape, dtype=np.int8)
    out[arr > threshold] = 1
    out[arr < -threshold] = -1
    return out


def prepare_secbar() -> tuple[pd.DataFrame, pd.Series]:
    df = pdq.load_wide()
    df = df.sort_values("sec", kind="mergesort").reset_index(drop=True)
    df["idx"] = np.arange(len(df), dtype=np.int64)
    df = pdq.add_pdq_features(df)
    df = pdq.add_completed_bar_indicators(df)
    df = pdq.add_opening_range(df)
    mask = pdq.build_opportunity_mask(df)
    df["pdq_cont_mask"] = mask.astype(np.int8)
    df["cross_sync_count"] = 0
    for root in ROOTS:
        df["cross_sync_count"] += (
            signed(df[f"nimp60_{root}"]) == df["signC60"].to_numpy()
        ).astype(np.int8)
        df[f"e60_{root}"] = df[f"nimp60_{root}"] - df["C60"]

    tpe_dt = pd.to_datetime(df["sec"], unit="s", utc=True).dt.tz_convert("Asia/Taipei")
    minute_of_day = tpe_dt.dt.hour * 60 + tpe_dt.dt.minute
    df["tpe_time"] = tpe_dt.astype(str)
    df["session"] = np.select(
        [
            (minute_of_day >= 8 * 60 + 45) & (minute_of_day <= 13 * 60 + 45),
            (minute_of_day >= 15 * 60) | (minute_of_day <= 5 * 60),
        ],
        ["day", "night"],
        default="off",
    )
    return df, mask


def build_canonical_events(df: pd.DataFrame, mask: pd.Series) -> pd.DataFrame:
    prev_mask = mask.shift(1, fill_value=False)
    prev_sec = df["sec"].shift(1)
    new_run = mask & (~prev_mask | ((df["sec"] - prev_sec) > STEP_S))
    ev = df.loc[
        new_run,
        [
            "idx",
            "sec",
            "day",
            "split",
            "session",
            "hour_tpe",
            "tpe_time",
            "C60",
            "rvexp",
            "cross_sync_count",
            "zlogL_min",
            "spread_agg",
            "d5_agg",
            "e60_TXF",
            "e60_TMF",
            "dir_tsi15",
            "dir_or",
            "signC60",
        ],
    ].copy()
    ev = ev.rename(
        columns={
            "sec": "t0",
            "C60": "C60_t0",
            "rvexp": "RVExp_t0",
            "cross_sync_count": "CrossSync_t0",
            "zlogL_min": "zLogL_t0",
            "spread_agg": "spread_t0",
            "d5_agg": "D5_t0",
            "e60_TXF": "e_TXF_t0",
            "e60_TMF": "e_TMF_t0",
            "dir_tsi15": "TSI15_dir",
            "dir_or": "OR_bias",
            "signC60": "entry_dir_C",
        }
    )
    ev["event_id"] = [f"PDQ{n:06d}" for n in range(1, len(ev) + 1)]
    ev["abs_C60"] = ev["C60_t0"].abs()
    ev["entry_dir_TSI"] = ev["TSI15_dir"].fillna(0).astype(np.int8)
    ev["TSI_C_state"] = np.select(
        [
            (ev["entry_dir_TSI"] != 0) & (ev["entry_dir_TSI"] == ev["entry_dir_C"]),
            (ev["entry_dir_TSI"] != 0) & (ev["entry_dir_TSI"] == -ev["entry_dir_C"]),
        ],
        ["aligned", "opposite"],
        default="tsi_no_dir",
    )
    return ev.reset_index(drop=True)


def extract_event_windows(df: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    secs = df["sec"].to_numpy(dtype=np.int64)
    rows = []
    cols = [
        "sec",
        "mid_pts_TXF",
        "mid_pts_MXF",
        "mid_pts_TMF",
        "mid_agg",
        "C60",
        "e60_TXF",
        "e60_TMF",
        "rvexp",
        "cross_sync_count",
        "zlogL_min",
        "spread_agg",
        "d5_agg",
    ]
    for ev in events.itertuples(index=False):
        start = int(np.searchsorted(secs, ev.t0 - PRE_S, side="left"))
        end = int(np.searchsorted(secs, ev.t0 + POST_S, side="right"))
        w = df.iloc[start:end][cols].copy()
        if w.empty:
            continue
        w["event_id"] = ev.event_id
        w["dt"] = (w["sec"] - ev.t0).astype(np.int16)
        w = w[(w["dt"] >= -PRE_S) & (w["dt"] <= POST_S)]
        w["ret_TXF"] = w["mid_pts_TXF"] - float(df.at[ev.idx, "mid_pts_TXF"])
        w["ret_MXF"] = w["mid_pts_MXF"] - float(df.at[ev.idx, "mid_pts_MXF"])
        w["ret_TMF"] = w["mid_pts_TMF"] - float(df.at[ev.idx, "mid_pts_TMF"])
        w["ret_common"] = w["mid_agg"] - float(df.at[ev.idx, "mid_agg"])
        w["signed_ret_C"] = int(ev.entry_dir_C) * w["ret_common"]
        tsi_dir = int(ev.entry_dir_TSI) if not pd.isna(ev.entry_dir_TSI) else 0
        w["signed_ret_TSI"] = np.where(tsi_dir == 0, np.nan, tsi_dir * w["ret_common"])
        rows.append(w)
    if not rows:
        return pd.DataFrame()
    out = pd.concat(rows, ignore_index=True)
    out = out.rename(
        columns={
            "mid_pts_TXF": "mid_TXF",
            "mid_pts_MXF": "mid_MXF",
            "mid_pts_TMF": "mid_TMF",
            "mid_agg": "mid_common",
            "rvexp": "RVExp",
            "cross_sync_count": "CrossSync",
            "zlogL_min": "zLogL",
            "spread_agg": "spread",
            "d5_agg": "D5",
            "e60_TXF": "e_TXF",
            "e60_TMF": "e_TMF",
        }
    )
    return out.sort_values(["event_id", "dt"], kind="mergesort").reset_index(drop=True)


def first_touch(dt: np.ndarray, ret: np.ndarray, threshold: float, side: str) -> float:
    if side == "up":
        idx = np.where(ret >= threshold)[0]
    else:
        idx = np.where(ret <= threshold)[0]
    return float(dt[idx[0]]) if len(idx) else np.nan


def label_one(event_id: str, w: pd.DataFrame) -> dict[str, float | str | bool]:
    post = w[(w["dt"] >= 0) & (w["dt"] <= POST_S)].copy()
    dt = post["dt"].to_numpy(dtype=float)
    raw = post["ret_common"].to_numpy(dtype=float)
    signed_c = post["signed_ret_C"].to_numpy(dtype=float)
    if len(post) == 0:
        return {"event_id": event_id, "path_label": "empty", "trade_path_label_C": "empty"}

    t_up8 = first_touch(dt, raw, UP_TH, "up")
    t_down8 = first_touch(dt, raw, DOWN_TH, "down")
    max_ret = float(np.nanmax(raw))
    min_ret = float(np.nanmin(raw))
    final_ret = float(raw[-1])
    two_sided = bool(max_ret >= UP_TH and min_ret <= DOWN_TH)
    if math.isnan(t_up8) and math.isnan(t_down8):
        first_label = "no_hit"
    elif not math.isnan(t_up8) and math.isnan(t_down8):
        first_label = "up8_only"
    elif math.isnan(t_up8) and not math.isnan(t_down8):
        first_label = "down8_only"
    elif t_up8 < t_down8:
        first_label = "up8_first"
    elif t_down8 < t_up8:
        first_label = "down8_first"
    else:
        first_label = "tie"

    if first_label.startswith("up") and min_ret >= -4 and final_ret >= UP_TH:
        path_label = "clean_up"
    elif first_label.startswith("down") and max_ret <= 4 and final_ret <= DOWN_TH:
        path_label = "clean_down"
    elif two_sided:
        path_label = "two_sided"
    elif first_label.startswith("up") and (final_ret <= 0 or max_ret - final_ret >= 16):
        path_label = "fake_up"
    elif first_label.startswith("down") and (final_ret >= 0 or final_ret - min_ret >= 16):
        path_label = "fake_down"
    else:
        path_label = first_label

    t_signed_up8 = first_touch(dt, signed_c, UP_TH, "up")
    t_signed_down8 = first_touch(dt, signed_c, DOWN_TH, "down")
    mfe_c = float(np.nanmax(signed_c))
    mae_c = float(np.nanmin(signed_c))
    final_signed_c = float(signed_c[-1])
    t_mfe_c = float(dt[int(np.nanargmax(signed_c))])
    t_mae_c = float(dt[int(np.nanargmin(signed_c))])
    before_mfe = signed_c[dt <= t_mfe_c]
    mae_before_mfe = float(np.nanmin(before_mfe)) if len(before_mfe) else np.nan
    signed_two_sided = bool(mfe_c >= UP_TH and mae_c <= DOWN_TH)
    if math.isnan(t_signed_up8) and math.isnan(t_signed_down8):
        trade_label = "no_hit"
    elif not math.isnan(t_signed_down8) and (
        math.isnan(t_signed_up8) or t_signed_down8 < t_signed_up8
    ):
        trade_label = "wrong_way_first"
    elif signed_two_sided:
        trade_label = "two_sided_chop"
    elif not math.isnan(t_signed_up8) and mae_c >= -4 and final_signed_c >= UP_TH:
        trade_label = "clean_continuation"
    elif not math.isnan(t_signed_up8) and (
        final_signed_c <= 0 or mfe_c - final_signed_c >= 16
    ):
        trade_label = "fake_continuation"
    elif not math.isnan(t_signed_up8):
        trade_label = "messy_continuation"
    else:
        trade_label = "other"

    return {
        "event_id": event_id,
        "t_up8": t_up8,
        "t_down8": t_down8,
        "first_passage_label": first_label,
        "path_label": path_label,
        "two_sided": two_sided,
        "max_ret": max_ret,
        "min_ret": min_ret,
        "final_ret": final_ret,
        "t_signed_up8_C": t_signed_up8,
        "t_signed_down8_C": t_signed_down8,
        "mfe_C": mfe_c,
        "mae_C": mae_c,
        "final_signed_C": final_signed_c,
        "t_mfe_C": t_mfe_c,
        "t_mae_C": t_mae_c,
        "mae_before_mfe_C": mae_before_mfe,
        "trade_path_label_C": trade_label,
        "window_complete_900s": bool(np.nanmax(dt) >= POST_S),
    }


def label_paths(windows: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    labels = [label_one(eid, w) for eid, w in windows.groupby("event_id", sort=False)]
    labels_df = pd.DataFrame(labels)
    return events.merge(labels_df, on="event_id", how="left", validate="one_to_one")


def summarize_labels(events: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows = []
    for keys, sub in events.groupby(["split", "session", "trade_path_label_C"], dropna=False):
        split, session, label = keys
        rows.append(
            {
                "split": split,
                "session": session,
                "trade_path_label_C": label,
                "n": len(sub),
                "share": len(sub) / max(1, len(events[events["split"] == split])),
                "mfe_C_mean": sub["mfe_C"].mean(),
                "mae_C_mean": sub["mae_C"].mean(),
                "final_signed_C_mean": sub["final_signed_C"].mean(),
                "two_sided_rate": sub["two_sided"].mean(),
            }
        )
    label_summary = pd.DataFrame(rows)

    hour = (
        events.groupby(["split", "hour_tpe", "trade_path_label_C"], dropna=False)
        .size()
        .reset_index(name="n")
    )
    totals = hour.groupby(["split", "hour_tpe"])["n"].transform("sum")
    hour["share"] = hour["n"] / totals
    return label_summary, hour


def first_passage_curves(events: pd.DataFrame) -> pd.DataFrame:
    groups = {"all": pd.Series(True, index=events.index)}
    groups["TSI_C_aligned"] = events["TSI_C_state"].eq("aligned")
    groups["TSI_C_opposite"] = events["TSI_C_state"].eq("opposite")
    groups["TSI_no_dir"] = events["TSI_C_state"].eq("tsi_no_dir")
    groups["session_day"] = events["session"].eq("day")
    groups["session_night"] = events["session"].eq("night")
    for hour in (9, 16, 22, 23):
        groups[f"hour_{hour}"] = events["hour_tpe"].eq(hour)

    rows = []
    grid = np.arange(0, POST_S + STEP_S, STEP_S)
    for name, mask in groups.items():
        sub = events[mask]
        if sub.empty:
            continue
        for t in grid:
            rows.append(
                {
                    "group": name,
                    "t": int(t),
                    "n": int(len(sub)),
                    "raw_up8_touched": float((sub["t_up8"].notna() & (sub["t_up8"] <= t)).mean()),
                    "raw_down8_touched": float(
                        (sub["t_down8"].notna() & (sub["t_down8"] <= t)).mean()
                    ),
                    "signed_plus8_touched": float(
                        (sub["t_signed_up8_C"].notna() & (sub["t_signed_up8_C"] <= t)).mean()
                    ),
                    "signed_minus8_touched": float(
                        (sub["t_signed_down8_C"].notna() & (sub["t_signed_down8_C"] <= t)).mean()
                    ),
                }
            )
    return pd.DataFrame(rows)


CSS = """
body{font-family:Arial,Helvetica,sans-serif;margin:24px;color:#17202a;background:#fbfcfd}
a{color:#0b63ce} table{border-collapse:collapse;margin:12px 0;font-size:13px}
th,td{border:1px solid #d7dde5;padding:5px 7px;text-align:right} th{text-align:left;background:#eef2f6}
.note{max-width:980px;line-height:1.5}.panel{margin:18px 0;padding:12px;border:1px solid #dce2ea;background:white}
canvas{border:1px solid #c8d0da;image-rendering:pixelated;max-width:100%}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:12px}
svg{background:white;border:1px solid #dce2ea}.small{font-size:12px;color:#5f6b7a}
"""


def write_html(path: Path, title: str, body: str) -> None:
    path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
        f"<body><h1>{html.escape(title)}</h1>{body}</body></html>"
    )


def color_for_label(label: str) -> str:
    colors = {
        "clean_continuation": "#1a9850",
        "messy_continuation": "#91cf60",
        "fake_continuation": "#fdae61",
        "two_sided_chop": "#984ea3",
        "wrong_way_first": "#d73027",
        "no_hit": "#7f8c8d",
        "other": "#4575b4",
    }
    return colors.get(str(label), "#4575b4")


def make_heatmap(
    windows: pd.DataFrame,
    events: pd.DataFrame,
    value_col: str,
    sort_by: str,
    filename: str,
    title: str,
) -> None:
    meta = events[["event_id", "trade_path_label_C", "hour_tpe", "final_signed_C", "t_mfe_C"]]
    if sort_by == "final":
        order = meta.sort_values("final_signed_C")["event_id"].to_list()
    elif sort_by == "t_mfe":
        order = meta.sort_values(["t_mfe_C", "final_signed_C"])["event_id"].to_list()
    elif sort_by == "hour":
        order = meta.sort_values(["hour_tpe", "trade_path_label_C", "final_signed_C"])[
            "event_id"
        ].to_list()
    else:
        label_order = {
            "clean_continuation": 0,
            "messy_continuation": 1,
            "fake_continuation": 2,
            "two_sided_chop": 3,
            "wrong_way_first": 4,
            "no_hit": 5,
        }
        temp = meta.assign(
            label_rank=meta["trade_path_label_C"].map(label_order).fillna(9)
        )
        order = temp.sort_values(["label_rank", "final_signed_C"])["event_id"].to_list()

    mat = (
        windows[windows["event_id"].isin(order)]
        .pivot_table(index="event_id", columns="dt", values=value_col, aggfunc="last")
        .reindex(order)
    )
    mat = mat.reindex(columns=list(range(-PRE_S, POST_S + STEP_S, STEP_S)))
    values = np.clip(mat.to_numpy(dtype=float), -80, 80)
    values = np.where(np.isnan(values), None, np.round(values, 2)).tolist()
    cols = [int(c) for c in mat.columns]
    labels = events.set_index("event_id").loc[mat.index, "trade_path_label_C"].astype(str).to_list()
    body = f"""
    <p class='note'>Rows are canonical PDQ_cont run-start events. Columns are seconds from t0.
    Color is clipped to [-80,+80] points for readability. Sort: {html.escape(sort_by)}.</p>
    <canvas id='hm'></canvas>
    <div class='small'>Rows: {len(mat)}, Columns: {len(cols)}, Value: {html.escape(value_col)}</div>
    <script>
    const vals = {json.dumps(values)};
    const cols = {json.dumps(cols)};
    const labels = {json.dumps(labels)};
    const canvas = document.getElementById('hm');
    const rows = vals.length, cw = vals[0].length;
    canvas.width = cw; canvas.height = rows;
    canvas.style.width = Math.min(1200, cw*5) + 'px';
    canvas.style.height = Math.min(1600, Math.max(300, rows)) + 'px';
    const ctx = canvas.getContext('2d');
    const img = ctx.createImageData(cw, rows);
    function color(v){{
      if(v === null) return [235,238,242,255];
      const x = Math.max(-1, Math.min(1, v/80));
      if(x >= 0){{
        return [Math.round(245-165*x), Math.round(245-95*x), Math.round(245-205*x), 255];
      }}
      const y = -x;
      return [Math.round(245-195*y), Math.round(245-125*y), Math.round(245-20*y), 255];
    }}
    for(let r=0;r<rows;r++){{
      for(let c=0;c<cw;c++){{
        const rgba = color(vals[r][c]);
        const k = 4*(r*cw+c);
        img.data[k]=rgba[0]; img.data[k+1]=rgba[1]; img.data[k+2]=rgba[2]; img.data[k+3]=rgba[3];
      }}
    }}
    ctx.putImageData(img,0,0);
    </script>
    """
    write_html(OUT_DIR / filename, title, body)


def scale(values: Iterable[float], lo: float, hi: float, px0: float, px1: float) -> list[float]:
    vals = list(values)
    if not np.isfinite(lo) or not np.isfinite(hi) or hi == lo:
        return [(px0 + px1) / 2 for _ in vals]
    return [px1 - (float(v) - lo) / (hi - lo) * (px1 - px0) if np.isfinite(v) else np.nan for v in vals]


def polyline(points: list[tuple[float, float]], color: str, width: float = 1.4) -> str:
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in points if np.isfinite(x) and np.isfinite(y))
    return f"<polyline fill='none' stroke='{color}' stroke-width='{width}' points='{pts}'/>"


def scatter_svg(events: pd.DataFrame) -> str:
    w, h, pad = 860, 560, 56
    xvals = -events["mae_before_mfe_C"].astype(float)
    yvals = events["mfe_C"].astype(float)
    xlo, xhi = 0.0, float(np.nanpercentile(xvals, 98))
    ylo, yhi = 0.0, float(np.nanpercentile(yvals, 98))
    xs = scale(xvals, xlo, xhi, pad, w - pad)
    ys = scale(yvals, ylo, yhi, pad, h - pad)
    parts = [
        f"<svg width='{w}' height='{h}' viewBox='0 0 {w} {h}'>",
        f"<line x1='{pad}' y1='{h-pad}' x2='{w-pad}' y2='{h-pad}' stroke='#53606f'/>",
        f"<line x1='{pad}' y1='{pad}' x2='{pad}' y2='{h-pad}' stroke='#53606f'/>",
        f"<text x='{w/2}' y='{h-12}' text-anchor='middle'>MAE before MFE, positive adverse pts</text>",
        f"<text x='18' y='{h/2}' transform='rotate(-90 18,{h/2})' text-anchor='middle'>MFE pts</text>",
    ]
    for x, y, label, hour in zip(xs, ys, events["trade_path_label_C"], events["hour_tpe"]):
        if np.isfinite(x) and np.isfinite(y):
            parts.append(
                f"<circle cx='{x:.1f}' cy='{y:.1f}' r='3' fill='{color_for_label(label)}' "
                f"fill-opacity='0.65'><title>{html.escape(str(label))} hour={int(hour)}</title></circle>"
            )
    parts.append("</svg>")
    return "\n".join(parts)


def make_scatter(events: pd.DataFrame) -> None:
    legend = " ".join(
        f"<span style='color:{color_for_label(k)}'>● {k}</span>"
        for k in sorted(events["trade_path_label_C"].dropna().unique())
    )
    body = f"<p class='note'>Each point is one canonical event. x is adverse excursion before MFE, y is MFE, both direction-aligned by sign(C).</p><p>{legend}</p>{scatter_svg(events)}"
    write_html(OUT_DIR / "mfe_mae_scatter.html", "PDQ_cont MFE / MAE Scatter", body)


def make_first_passage_html(curves: pd.DataFrame) -> None:
    groups = ["all", "TSI_C_aligned", "TSI_C_opposite", "TSI_no_dir", "session_day", "session_night"]
    colors = ["#111827", "#1a9850", "#d73027", "#7f8c8d", "#2b6cb0", "#984ea3"]
    w, h, pad = 920, 520, 58
    parts = [
        f"<svg width='{w}' height='{h}' viewBox='0 0 {w} {h}'>",
        f"<line x1='{pad}' y1='{h-pad}' x2='{w-pad}' y2='{h-pad}' stroke='#53606f'/>",
        f"<line x1='{pad}' y1='{pad}' x2='{pad}' y2='{h-pad}' stroke='#53606f'/>",
        f"<text x='{w/2}' y='{h-12}' text-anchor='middle'>seconds from t0</text>",
        f"<text x='18' y='{h/2}' transform='rotate(-90 18,{h/2})' text-anchor='middle'>touch probability</text>",
    ]
    for group, color in zip(groups, colors):
        sub = curves[curves["group"].eq(group)]
        if sub.empty:
            continue
        xs = scale(sub["t"], 0, POST_S, pad, w - pad)
        ys1 = scale(sub["signed_plus8_touched"], 0, 1, pad, h - pad)
        ys2 = scale(sub["signed_minus8_touched"], 0, 1, pad, h - pad)
        parts.append(polyline(list(zip(xs, ys1)), color, 2.0))
        parts.append(polyline(list(zip(xs, ys2)), color, 1.0).replace("stroke-width='1.0'", "stroke-width='1.0' stroke-dasharray='4 4'"))
        parts.append(f"<text x='{w-pad-150}' y='{pad+18*groups.index(group)}' fill='{color}'>solid +8 {group}; dash -8</text>")
    parts.append("</svg>")
    body = "<p class='note'>Solid lines are direction-aligned +8 touched by time t; dashed lines are direction-aligned -8 touched by time t.</p>" + "\n".join(parts)
    write_html(OUT_DIR / "first_passage_curves.html", "PDQ_cont First-Passage Curves", body)


def phase_svg(events: pd.DataFrame, x_col: str, y_col: str, title: str) -> str:
    w, h, pad = 540, 420, 48
    x = events[x_col].astype(float)
    y = events[y_col].astype(float)
    xlo, xhi = np.nanpercentile(x, [1, 99])
    ylo, yhi = np.nanpercentile(y, [1, 99])
    xs = scale(x.clip(xlo, xhi), xlo, xhi, pad, w - pad)
    ys = scale(y.clip(ylo, yhi), ylo, yhi, pad, h - pad)
    parts = [
        f"<svg width='{w}' height='{h}' viewBox='0 0 {w} {h}'>",
        f"<text x='{w/2}' y='24' text-anchor='middle'>{html.escape(title)}</text>",
        f"<line x1='{pad}' y1='{h-pad}' x2='{w-pad}' y2='{h-pad}' stroke='#53606f'/>",
        f"<line x1='{pad}' y1='{pad}' x2='{pad}' y2='{h-pad}' stroke='#53606f'/>",
        f"<text x='{w/2}' y='{h-10}' text-anchor='middle'>{html.escape(x_col)}</text>",
        f"<text x='16' y='{h/2}' transform='rotate(-90 16,{h/2})' text-anchor='middle'>{html.escape(y_col)}</text>",
    ]
    for xi, yi, label in zip(xs, ys, events["trade_path_label_C"]):
        if np.isfinite(xi) and np.isfinite(yi):
            parts.append(f"<circle cx='{xi:.1f}' cy='{yi:.1f}' r='2.6' fill='{color_for_label(label)}' fill-opacity='0.6'/>")
    parts.append("</svg>")
    return "\n".join(parts)


def make_phase_plots(events: pd.DataFrame) -> None:
    body = "<p class='note'>Points are t0 event states, colored by direction-aligned path label.</p><div class='grid'>"
    body += phase_svg(events, "C60_t0", "e_TXF_t0", "C60 vs e_TXF")
    body += phase_svg(events, "RVExp_t0", "CrossSync_t0", "RVExp vs CrossSync")
    body += phase_svg(events, "zLogL_t0", "spread_t0", "zLogL vs spread")
    body += "</div>"
    write_html(OUT_DIR / "phase_plots.html", "PDQ_cont Phase Plots", body)


def make_hour_label_heatmap(hour: pd.DataFrame) -> None:
    pivot = hour.pivot_table(
        index=["split", "hour_tpe"],
        columns="trade_path_label_C",
        values="share",
        aggfunc="sum",
        fill_value=0,
    )
    count = hour.pivot_table(
        index=["split", "hour_tpe"],
        values="n",
        aggfunc="sum",
        fill_value=0,
    )
    table = pivot.join(count.rename(columns={"n": "N"})).reset_index()
    table.to_csv(OUT_DIR / "hour_path_label_heatmap.csv", index=False)
    body = "<p class='note'>Cells are share of path labels within split-hour. N is event count.</p>"
    body += table.round(3).to_html(index=False, escape=True)
    write_html(OUT_DIR / "hour_path_label_heatmap.html", "Hour x Path Label Heatmap", body)


def normalize_panel(series: pd.Series) -> pd.Series:
    lo, hi = series.quantile(0.05), series.quantile(0.95)
    if not np.isfinite(lo) or not np.isfinite(hi) or hi == lo:
        return pd.Series(0.5, index=series.index)
    return ((series.clip(lo, hi) - lo) / (hi - lo)).clip(0, 1)


def replay_svg(w: pd.DataFrame, event_id: str, label: str) -> str:
    width, panel_h, pad = 520, 115, 30
    height = panel_h * 4 + 42
    x = w["dt"].to_numpy(dtype=float)
    xs = scale(x, -PRE_S, POST_S, pad, width - pad)
    panels = [
        ("Price ret", [("common", "ret_common", "#111827"), ("TXF", "ret_TXF", "#1f78b4"), ("MXF", "ret_MXF", "#33a02c"), ("TMF", "ret_TMF", "#e31a1c")]),
        ("PDQ comp", [("C60", "C60", "#111827"), ("eTXF", "e_TXF", "#1f78b4"), ("eTMF", "e_TMF", "#e31a1c")]),
        ("Vol/sync normalized", [("RVExp", "RVExp_norm", "#ff7f00"), ("CrossSync", "CrossSync_norm", "#6a3d9a")]),
        ("Exec normalized", [("zLogL", "zLogL_norm", "#1f78b4"), ("spread", "spread_norm", "#e31a1c"), ("D5", "D5_norm", "#33a02c")]),
    ]
    temp = w.copy()
    for col in ("RVExp", "CrossSync", "zLogL", "spread", "D5"):
        temp[f"{col}_norm"] = normalize_panel(temp[col])
    parts = [f"<svg width='{width}' height='{height}' viewBox='0 0 {width} {height}'>"]
    parts.append(f"<text x='8' y='16'>{html.escape(event_id)} {html.escape(label)}</text>")
    for i, (name, specs) in enumerate(panels):
        y0 = 24 + i * panel_h
        y1 = y0 + panel_h - 18
        parts.append(f"<rect x='{pad}' y='{y0}' width='{width-2*pad}' height='{panel_h-18}' fill='#fff' stroke='#d7dde5'/>")
        parts.append(f"<text x='6' y='{y0+14}' font-size='10'>{html.escape(name)}</text>")
        parts.append(f"<line x1='{pad + (0 + PRE_S)/(PRE_S+POST_S)*(width-2*pad):.1f}' y1='{y0}' x2='{pad + (0 + PRE_S)/(PRE_S+POST_S)*(width-2*pad):.1f}' y2='{y1}' stroke='#111' stroke-dasharray='3 3'/>")
        values = []
        for _, col, _ in specs:
            values.extend(temp[col].astype(float).replace([np.inf, -np.inf], np.nan).dropna().to_list())
        lo, hi = (min(values), max(values)) if values else (-1, 1)
        if lo == hi:
            lo, hi = lo - 1, hi + 1
        if i == 0:
            lo, hi = min(lo, -8), max(hi, 8)
            y_up = scale([8], lo, hi, y0 + 4, y1 - 4)[0]
            y_dn = scale([-8], lo, hi, y0 + 4, y1 - 4)[0]
            parts.append(f"<line x1='{pad}' y1='{y_up:.1f}' x2='{width-pad}' y2='{y_up:.1f}' stroke='#999' stroke-dasharray='2 2'/>")
            parts.append(f"<line x1='{pad}' y1='{y_dn:.1f}' x2='{width-pad}' y2='{y_dn:.1f}' stroke='#999' stroke-dasharray='2 2'/>")
        for series_name, col, color in specs:
            ys = scale(temp[col], lo, hi, y0 + 4, y1 - 4)
            parts.append(polyline(list(zip(xs, ys)), color, 1.2))
        legend = " ".join(f"<tspan fill='{color}'>{series_name}</tspan>" for series_name, _, color in specs)
        parts.append(f"<text x='{pad+4}' y='{y1-4}' font-size='10'>{legend}</text>")
    parts.append("</svg>")
    return "\n".join(parts)


def make_replay_gallery(windows: pd.DataFrame, events: pd.DataFrame) -> None:
    labels = ["clean_continuation", "fake_continuation", "two_sided_chop", "wrong_way_first"]
    body = "<p class='note'>Each card is one event replay. Vol/sync and execution panels are normalized per event for shape comparison.</p>"
    window_map = {event_id: w for event_id, w in windows.groupby("event_id", sort=False)}
    for label in labels:
        sub = events[events["trade_path_label_C"].eq(label)].copy()
        if sub.empty:
            continue
        sub = sub.sort_values(["split", "hour_tpe", "event_id"]).head(20)
        body += f"<h2>{html.escape(label)} ({len(sub)} shown)</h2><div class='grid'>"
        for event_id in sub["event_id"]:
            w = window_map.get(event_id)
            if w is None:
                continue
            body += replay_svg(w, event_id, label)
        body += "</div>"
    write_html(OUT_DIR / "event_replay_gallery.html", "PDQ_cont Event Replay Gallery", body)


def make_index(events: pd.DataFrame, label_summary: pd.DataFrame) -> None:
    links = [
        "heatmap_signed_ret_by_label.html",
        "heatmap_signed_ret_by_final.html",
        "heatmap_raw_ret_by_hour.html",
        "heatmap_signed_ret_by_tmfe.html",
        "event_replay_gallery.html",
        "mfe_mae_scatter.html",
        "first_passage_curves.html",
        "phase_plots.html",
        "hour_path_label_heatmap.html",
    ]
    body = "<p class='note'>Canonical PDQ_cont atlas. Event set is signal-run starts from the saved PDQ_cont mask; all downstream plots use these event IDs.</p>"
    body += "<ul>" + "".join(f"<li><a href='{x}'>{x}</a></li>" for x in links) + "</ul>"
    body += "<h2>Event Counts</h2>"
    body += events.groupby(["split", "session"]).size().reset_index(name="n").to_html(index=False)
    body += "<h2>Path Label Summary</h2>"
    body += label_summary.round(4).to_html(index=False)
    write_html(OUT_DIR / "index.html", "PDQ_cont Event Atlas", body)


def write_notes(events: pd.DataFrame) -> None:
    counts = events["trade_path_label_C"].value_counts().to_dict()
    lines = [
        "# PDQ_cont Event Atlas",
        "",
        f"- Source: `{pdq.DATA_PATH.relative_to(ROOT)}`",
        f"- Window: t0-{PRE_S}s to t0+{POST_S}s",
        f"- Cadence: {STEP_S}s common rows from exported secbar",
        f"- Canonical events: {len(events)} PDQ_cont signal-run starts",
        f"- Path label counts: `{counts}`",
        "",
        "Event definition is fixed in `canonical_events.parquet` and `canonical_events.csv`.",
        "Use raw return plots for market structure and signed-return plots for sign(C) trading path.",
    ]
    (OUT_DIR / "notes.md").write_text("\n".join(lines))


def main() -> None:
    ensure_dir()
    df, mask = prepare_secbar()
    events = build_canonical_events(df, mask)
    windows = extract_event_windows(df, events)
    events = label_paths(windows, events)
    label_summary, hour_summary = summarize_labels(events)
    curves = first_passage_curves(events)

    events.to_csv(OUT_DIR / "event_labels.csv", index=False)
    events.to_csv(OUT_DIR / "canonical_events.csv", index=False)
    label_summary.to_csv(OUT_DIR / "path_label_summary.csv", index=False)
    curves.to_csv(OUT_DIR / "first_passage_curves.csv", index=False)
    write_parquet(events, OUT_DIR / "canonical_events.parquet")
    write_parquet(windows.head(200_000), OUT_DIR / "event_windows_sample.parquet")

    make_heatmap(windows, events, "signed_ret_C", "path_label", "heatmap_signed_ret_by_label.html", "Signed Return Heatmap Sorted by Path Label")
    make_heatmap(windows, events, "signed_ret_C", "final", "heatmap_signed_ret_by_final.html", "Signed Return Heatmap Sorted by Final 900s Return")
    make_heatmap(windows, events, "signed_ret_C", "t_mfe", "heatmap_signed_ret_by_tmfe.html", "Signed Return Heatmap Sorted by T_MFE")
    make_heatmap(windows, events, "ret_common", "hour", "heatmap_raw_ret_by_hour.html", "Raw Return Heatmap Sorted by Hour")
    make_scatter(events)
    make_first_passage_html(curves)
    make_phase_plots(events)
    make_hour_label_heatmap(hour_summary)
    make_replay_gallery(windows, events)
    make_index(events, label_summary)
    write_notes(events)

    windows_csv = OUT_DIR / "event_windows.csv.gz"
    windows.to_csv(
        windows_csv,
        index=False,
        compression={"method": "gzip", "compresslevel": 1},
    )

    meta = {
        "source": str(pdq.DATA_PATH.relative_to(ROOT)),
        "output_dir": str(OUT_DIR.relative_to(ROOT)),
        "rows_common": int(len(df)),
        "canonical_event_count": int(len(events)),
        "window_rows": int(len(windows)),
        "pre_seconds": PRE_S,
        "post_seconds": POST_S,
        "heatmap_grid_seconds": STEP_S,
        "event_windows_time_unit": "actual seconds from exported common rows",
        "event_definition": {
            "name": "pdq_cont_run_start_rebuilt_q95",
            "mask": [
                "abs(C60) > split q95",
                "RVExp > split q90",
                "CrossSync >= 2",
                "StableBook == false",
                "spread <= split q90",
                "D5 >= split q20",
                "sign(C60) != 0",
            ],
            "run_start": "mask true and previous row is false or gap > 5s",
        },
        "price_unit": "TAIFEX points",
        "split": {"IS": "2026-03-03..2026-04-30", "OOS": "2026-05-01..2026-06-13"},
        "bar_alignment": "TSI15 and OR use prior completed bars/ranges from base PDQ tool",
        "html": [
            "index.html",
            "heatmap_signed_ret_by_label.html",
            "heatmap_raw_ret_by_hour.html",
            "event_replay_gallery.html",
            "mfe_mae_scatter.html",
            "first_passage_curves.html",
            "phase_plots.html",
            "hour_path_label_heatmap.html",
        ],
        "window_storage_note": (
            "Full event windows are stored in event_windows.csv.gz. "
            "A 200k-row event_windows_sample.parquet is provided for quick parquet inspection; "
            "single-file full parquet conversion was avoided because it stalls in this sandbox."
        ),
    }
    (OUT_DIR / "metadata.json").write_text(json.dumps(meta, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
