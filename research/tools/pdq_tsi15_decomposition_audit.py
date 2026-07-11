"""PDQ_cont q95 + TSI15 decomposition audit.

This is a research-only audit over the exported front-month L2 secbar data.
It intentionally avoids adding a new parameter grid.  The goal is to test
whether the TSI15 result is a slow-trend proxy, an hour/session proxy, or a
concentrated sample artifact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
DATA_PATH = (
    ROOT
    / "outputs/liquidity_score/db_wide_validation/"
    / "front_month_secbar_l2_20260303_20260613.csv.gz"
)
OUT_DIR = ROOT / "outputs/liquidity_score/pdq_tsi15_decomposition_audit"
IS_END_DAY = "2026-04-30"
OOS_START_DAY = "2026-05-01"
COSTS = (2.0, 4.0, 6.0)
ROOT_WEIGHTS = {"TXF": 0.5, "MXF": 0.3, "TMF": 0.2}
ROOTS = ("TXF", "MXF", "TMF")
PRIOR_LF_BASELINE_COUNTS = {"IS": 2114, "OOS": 2004}


@dataclass(frozen=True)
class ExitResult:
    pnl: np.ndarray
    hold_s: np.ndarray
    reason: np.ndarray


def signed(values: pd.Series | np.ndarray, threshold: float = 0.0) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    out = np.zeros(arr.shape, dtype=np.int8)
    out[arr > threshold] = 1
    out[arr < -threshold] = -1
    return out


def split_name(day: str) -> str:
    if day <= IS_END_DAY:
        return "IS"
    if day >= OOS_START_DAY:
        return "OOS"
    return "GAP"


def summarize_events(
    events: pd.DataFrame,
    pnl_col: str,
    mfe_col: str = "mfe_300",
    hold_col: str | None = None,
) -> dict[str, float | int]:
    if events.empty:
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
            "hit_active_days": 0,
            "top5_day_abs_share": np.nan,
            "drop_top3_day_gross_mean": np.nan,
        }
    else:
        pnl = events[pnl_col].astype(float)
        daily = events.groupby("day", sort=True)[pnl_col].sum()
        abs_total = float(daily.abs().sum())
        top5_share = (
            float(daily.abs().nlargest(5).sum() / abs_total) if abs_total > 0 else np.nan
        )
        keep_days = daily.abs().sort_values(ascending=False).iloc[3:].index
        drop_top3 = events[events["day"].isin(keep_days)][pnl_col]
        row = {
            "n": int(len(events)),
            "active_days": int(events["day"].nunique()),
            "gross_mean": float(pnl.mean()),
            "hit_rate": float((pnl > 0).mean()),
            "p25": float(pnl.quantile(0.25)),
            "p50": float(pnl.quantile(0.50)),
            "p75": float(pnl.quantile(0.75)),
            "mfe_ge8_rate": float((events[mfe_col] >= 8.0).mean()),
            "mfe_p75": float(events[mfe_col].quantile(0.75)),
            "mfe_p90": float(events[mfe_col].quantile(0.90)),
            "median_daily_pnl": float(daily.median()),
            "hit_active_days": int((daily > 0).sum()),
            "top5_day_abs_share": top5_share,
            "drop_top3_day_gross_mean": float(drop_top3.mean()) if len(drop_top3) else np.nan,
        }
    if hold_col is not None and not events.empty:
        row["avg_hold_s"] = float(events[hold_col].mean())
    else:
        row["avg_hold_s"] = np.nan
    for cost in COSTS:
        row[f"net_mean_cost{int(cost)}"] = (
            row["gross_mean"] - cost if row["n"] else np.nan
        )
    return row


def load_wide() -> pd.DataFrame:
    cols = [
        "root",
        "day",
        "sec",
        "mid_pts",
        "bid5_qty",
        "ask5_qty",
        "d5_qty",
        "spread_pts",
        "logL",
        "quote_updates",
    ]
    raw = pd.read_csv(DATA_PATH, usecols=cols)
    pdf = raw.pivot_table(
        index=["day", "sec"],
        columns="root",
        values=[
            "mid_pts",
            "bid5_qty",
            "ask5_qty",
            "d5_qty",
            "spread_pts",
            "logL",
            "quote_updates",
        ],
        aggfunc="first",
    )
    pdf.columns = [f"{value}_{root}" for value, root in pdf.columns]
    pdf = pdf.reset_index()
    pdf = pdf.dropna(subset=[f"mid_pts_{r}" for r in ROOTS]).copy()
    pdf = pdf.sort_values(["day", "sec"], kind="mergesort").reset_index(drop=True)
    pdf["split"] = pdf["day"].map(split_name)
    pdf = pdf[pdf["split"].isin(["IS", "OOS"])].reset_index(drop=True)
    pdf["idx"] = np.arange(len(pdf), dtype=np.int64)
    pdf["hour_tpe"] = (
        pd.to_datetime(pdf["sec"], unit="s", utc=True)
        .dt.tz_convert("Asia/Taipei")
        .dt.hour.astype(np.int16)
    )
    pdf["minute_tpe"] = (
        pd.to_datetime(pdf["sec"], unit="s", utc=True)
        .dt.tz_convert("Asia/Taipei")
        .dt.minute.astype(np.int16)
    )
    return pdf


def add_pdq_features(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("day", sort=False)
    df["mid_agg"] = sum(ROOT_WEIGHTS[r] * df[f"mid_pts_{r}"] for r in ROOTS)
    for root in ROOTS:
        diff = g[f"mid_pts_{root}"].diff()
        imp60 = g[f"mid_pts_{root}"].diff(12)
        rv60 = diff.abs().groupby(df["day"], sort=False).rolling(12, min_periods=6).sum()
        rv60 = rv60.reset_index(level=0, drop=True).replace(0, np.nan)
        df[f"nimp60_{root}"] = imp60 / rv60
        df[f"rv60_{root}"] = rv60

        roll_mean = (
            df[f"logL_{root}"]
            .groupby(df["day"], sort=False)
            .rolling(240, min_periods=120)
            .mean()
            .reset_index(level=0, drop=True)
        )
        roll_std = (
            df[f"logL_{root}"]
            .groupby(df["day"], sort=False)
            .rolling(240, min_periods=120)
            .std()
            .reset_index(level=0, drop=True)
        )
        df[f"zlogL_{root}"] = (df[f"logL_{root}"] - roll_mean) / roll_std.replace(
            0, np.nan
        )

    df["C60"] = sum(ROOT_WEIGHTS[r] * df[f"nimp60_{r}"] for r in ROOTS)
    df["signC60"] = signed(df["C60"])
    df["rv60_agg"] = sum(ROOT_WEIGHTS[r] * df[f"rv60_{r}"] for r in ROOTS)
    rv_base = (
        df["rv60_agg"]
        .groupby(df["day"], sort=False)
        .rolling(240, min_periods=120)
        .median()
        .reset_index(level=0, drop=True)
    )
    df["rvexp"] = df["rv60_agg"] / rv_base.replace(0, np.nan)

    agree = np.zeros(len(df), dtype=np.int8)
    for root in ROOTS:
        agree += (signed(df[f"nimp60_{root}"]) == df["signC60"].to_numpy()).astype(
            np.int8
        )
    df["cross_sync_ge2"] = (agree >= 2).astype(np.int8)
    df["spread_agg"] = sum(ROOT_WEIGHTS[r] * df[f"spread_pts_{r}"] for r in ROOTS)
    df["d5_agg"] = sum(ROOT_WEIGHTS[r] * df[f"d5_qty_{r}"] for r in ROOTS)
    df["zlogL_min"] = np.nanmin(
        np.column_stack([df[f"zlogL_{r}"].to_numpy(dtype=float) for r in ROOTS]), axis=1
    )

    # Slow alternatives for Test 1.
    df["ret15m"] = g["mid_agg"].diff(180)
    df["deff15"] = df["ret15m"] / (
        g["mid_agg"].diff().abs().groupby(df["day"], sort=False).rolling(180, min_periods=90).sum()
        .reset_index(level=0, drop=True)
        .replace(0, np.nan)
    )
    df["cslow15"] = df["ret15m"] / (
        g["mid_agg"].diff().abs().groupby(df["day"], sort=False).rolling(180, min_periods=90).sum()
        .reset_index(level=0, drop=True)
        .replace(0, np.nan)
    )
    df["dir_ret15"] = signed(df["ret15m"])
    df["dir_deff15"] = signed(df["deff15"])
    df["dir_cslow15"] = signed(df["cslow15"])
    return df


def compute_tsi(close: pd.Series, long_span: int = 25, short_span: int = 13, sig_span: int = 7) -> pd.DataFrame:
    mom = close.diff()
    num = mom.ewm(span=long_span, adjust=False, min_periods=long_span).mean()
    num = num.ewm(span=short_span, adjust=False, min_periods=short_span).mean()
    den = mom.abs().ewm(span=long_span, adjust=False, min_periods=long_span).mean()
    den = den.ewm(span=short_span, adjust=False, min_periods=short_span).mean()
    tsi = 100.0 * num / den.replace(0, np.nan)
    sig = tsi.ewm(span=sig_span, adjust=False, min_periods=sig_span).mean()
    return pd.DataFrame({"tsi15": tsi, "tsi15_signal": sig})


def compute_fisher(close: pd.Series, high: pd.Series, low: pd.Series, length: int = 10) -> pd.DataFrame:
    hh = high.rolling(length, min_periods=length).max()
    ll = low.rolling(length, min_periods=length).min()
    raw = 2.0 * ((close - ll) / (hh - ll).replace(0, np.nan) - 0.5)
    raw = raw.clip(-0.999, 0.999).fillna(0.0)
    vals = np.zeros(len(raw), dtype=float)
    fish = np.zeros(len(raw), dtype=float)
    raw_arr = raw.to_numpy(dtype=float)
    for i in range(1, len(raw_arr)):
        vals[i] = np.clip(0.33 * raw_arr[i] + 0.67 * vals[i - 1], -0.999, 0.999)
        fish[i] = 0.5 * np.log((1 + vals[i]) / (1 - vals[i])) + 0.5 * fish[i - 1]
    out = pd.DataFrame({"fisher3": fish})
    out["fisher3_trigger"] = out["fisher3"].shift(1)
    return out


def add_completed_bar_indicators(df: pd.DataFrame) -> pd.DataFrame:
    bars15 = (
        df.assign(bar15=(df["sec"] // 900) * 900)
        .groupby("bar15", sort=True)
        .agg(close=("mid_agg", "last"))
        .reset_index()
    )
    tsi = compute_tsi(bars15["close"])
    bars15 = pd.concat([bars15, tsi], axis=1)
    bars15["ema13"] = bars15["close"].ewm(span=13, adjust=False, min_periods=13).mean()
    bars15["ema15_slope"] = bars15["ema13"] - bars15["ema13"].shift(1)
    bars15["dir_ema15"] = signed(bars15["ema15_slope"])
    bars15["dir_tsi15"] = 0
    bars15.loc[
        (bars15["tsi15"] > bars15["tsi15_signal"]) & (bars15["tsi15"] > 0),
        "dir_tsi15",
    ] = 1
    bars15.loc[
        (bars15["tsi15"] < bars15["tsi15_signal"]) & (bars15["tsi15"] < 0),
        "dir_tsi15",
    ] = -1
    bars15 = bars15.rename(columns={"bar15": "prev_bar15"})

    df["prev_bar15"] = (df["sec"] // 900 - 1) * 900
    df = df.merge(
        bars15[
            [
                "prev_bar15",
                "tsi15",
                "tsi15_signal",
                "dir_tsi15",
                "ema15_slope",
                "dir_ema15",
            ]
        ],
        on="prev_bar15",
        how="left",
        validate="many_to_one",
    )

    bars3 = (
        df.assign(bar3=(df["sec"] // 180) * 180)
        .groupby("bar3", sort=True)
        .agg(close=("mid_agg", "last"), high=("mid_agg", "max"), low=("mid_agg", "min"))
        .reset_index()
    )
    fisher = compute_fisher(bars3["close"], bars3["high"], bars3["low"])
    bars3 = pd.concat([bars3, fisher], axis=1)
    prev_fish = bars3["fisher3"].shift(1)
    prev_trig = bars3["fisher3_trigger"].shift(1)
    bars3["dir_fisher3"] = 0
    bars3.loc[
        (prev_fish <= prev_trig)
        & (bars3["fisher3"] > bars3["fisher3_trigger"])
        & (bars3["fisher3"] < 0.75),
        "dir_fisher3",
    ] = 1
    bars3.loc[
        (prev_fish >= prev_trig)
        & (bars3["fisher3"] < bars3["fisher3_trigger"])
        & (bars3["fisher3"] > -0.75),
        "dir_fisher3",
    ] = -1
    bars3 = bars3.rename(columns={"bar3": "prev_bar3"})
    df["prev_bar3"] = (df["sec"] // 180 - 1) * 180
    df = df.merge(
        bars3[["prev_bar3", "fisher3", "fisher3_trigger", "dir_fisher3"]],
        on="prev_bar3",
        how="left",
        validate="many_to_one",
    )
    return df


def add_opening_range(df: pd.DataFrame) -> pd.DataFrame:
    tpe_dt = pd.to_datetime(df["sec"], unit="s", utc=True).dt.tz_convert("Asia/Taipei")
    minute_of_day = tpe_dt.dt.hour * 60 + tpe_dt.dt.minute
    day_mask = (minute_of_day >= 9 * 60) & (minute_of_day < 9 * 60 + 30)
    ranges = (
        df[day_mask]
        .groupby("day", sort=True)
        .agg(or_high=("mid_agg", "max"), or_low=("mid_agg", "min"))
        .reset_index()
    )
    df = df.merge(ranges, on="day", how="left", validate="many_to_one")
    after_or = minute_of_day >= 9 * 60 + 30
    df["dir_or"] = 0
    df.loc[after_or & (df["mid_agg"] > df["or_high"]), "dir_or"] = 1
    df.loc[after_or & (df["mid_agg"] < df["or_low"]), "dir_or"] = -1
    return df


def build_opportunity_mask(df: pd.DataFrame) -> pd.Series:
    q_abs_c = df.groupby("split")["C60"].transform(lambda s: s.abs().quantile(0.95))
    q_rv = df.groupby("split")["rvexp"].transform(lambda s: s.quantile(0.90))
    q_spread = df.groupby("split")["spread_agg"].transform(lambda s: s.quantile(0.90))
    q_d5 = df.groupby("split")["d5_agg"].transform(lambda s: s.quantile(0.20))
    stable_book = (df["spread_agg"] <= df.groupby("split")["spread_agg"].transform("median")) & (
        df["d5_agg"] >= df.groupby("split")["d5_agg"].transform("median")
    )
    mask = (
        df["C60"].abs().gt(q_abs_c)
        & df["rvexp"].gt(q_rv)
        & df["cross_sync_ge2"].eq(1)
        & ~stable_book
        & df["spread_agg"].le(q_spread)
        & df["d5_agg"].ge(q_d5)
        & df["signC60"].ne(0)
    )
    return mask.fillna(False)


def density_match_prior_pdq_cont(df: pd.DataFrame, base_mask: pd.Series) -> pd.Series:
    """Match the prior LF scorecard event density without optimizing returns.

    The previous LF-filter audit did not persist event rows, but it did persist
    baseline PDQ_cont q95 counts.  This keeps the decomposition on the same
    density scale by taking the highest displacement-capacity rows inside the
    rebuilt PDQ candidate set.
    """
    out = pd.Series(False, index=df.index)
    score = df["C60"].abs() * df["rvexp"]
    for split, target_n in PRIOR_LF_BASELINE_COUNTS.items():
        eligible = df.index[base_mask & df["split"].eq(split) & score.notna()]
        if len(eligible) <= target_n:
            out.loc[eligible] = True
            continue
        chosen = score.loc[eligible].nlargest(target_n).index
        out.loc[chosen] = True
    return out


def path_arrays(df: pd.DataFrame, max_horizon_s: int = 900) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mids = df["mid_agg"].to_numpy(dtype=float)
    days = df["day"].to_numpy()
    horizon_steps = max_horizon_s // 5
    future = np.full((len(df), horizon_steps + 1), np.nan, dtype=float)
    same = np.zeros((len(df), horizon_steps + 1), dtype=bool)
    for step in range(horizon_steps + 1):
        if step == 0:
            future[:, step] = mids
            same[:, step] = True
            continue
        future[:-step, step] = mids[step:]
        same[:-step, step] = days[:-step] == days[step:]
    future[~same] = np.nan
    return mids, future, same


def add_path_metrics(events: pd.DataFrame, future: np.ndarray) -> pd.DataFrame:
    idx = events["idx"].to_numpy(dtype=np.int64)
    entry = events["entry_mid"].to_numpy(dtype=float)
    direction = events["direction"].to_numpy(dtype=float)
    for h in (180, 300, 600, 900):
        step = h // 5
        path = future[idx, : step + 1]
        signed_path = direction[:, None] * (path - entry[:, None])
        events[f"pnl_fixed_{h}"] = signed_path[:, step]
        events[f"mfe_{h}"] = np.nanmax(signed_path, axis=1)
        events[f"mae_{h}"] = np.nanmin(signed_path, axis=1)
    return events


def signal_invalidation_exit(
    df: pd.DataFrame,
    events: pd.DataFrame,
    direction_col: str,
    maxhold_s: int = 900,
    require_cols: tuple[str, ...] = ("signC60", "dir_tsi15"),
) -> ExitResult:
    max_steps = maxhold_s // 5
    idx_arr = events["idx"].to_numpy(dtype=np.int64)
    dir_arr = events["direction"].to_numpy(dtype=np.int8)
    entry = events["entry_mid"].to_numpy(dtype=float)
    days = df["day"].to_numpy()
    mids = df["mid_agg"].to_numpy(dtype=float)
    col_values = {col: df[col].to_numpy(dtype=np.int8) for col in require_cols}
    pnl = np.full(len(events), np.nan, dtype=float)
    hold = np.full(len(events), np.nan, dtype=float)
    reason = np.array(["timeout"] * len(events), dtype=object)
    for e, start in enumerate(idx_arr):
        end = min(start + max_steps, len(df) - 1)
        exit_idx = end
        exit_reason = "timeout"
        for pos in range(start + 1, end + 1):
            if days[pos] != days[start]:
                exit_idx = pos - 1
                exit_reason = "day_end"
                break
            invalid = False
            for col, values in col_values.items():
                if values[pos] != dir_arr[e]:
                    invalid = True
                    exit_reason = f"{col}_invalid"
                    break
            if invalid:
                exit_idx = pos
                break
        pnl[e] = dir_arr[e] * (mids[exit_idx] - entry[e])
        hold[e] = max(0, exit_idx - start) * 5.0
        reason[e] = exit_reason
    return ExitResult(pnl=pnl, hold_s=hold, reason=reason)


def or_invalidation_exit(df: pd.DataFrame, events: pd.DataFrame, maxhold_s: int = 900) -> ExitResult:
    max_steps = maxhold_s // 5
    idx_arr = events["idx"].to_numpy(dtype=np.int64)
    dir_arr = events["direction"].to_numpy(dtype=np.int8)
    entry = events["entry_mid"].to_numpy(dtype=float)
    days = df["day"].to_numpy()
    mids = df["mid_agg"].to_numpy(dtype=float)
    or_high = df["or_high"].to_numpy(dtype=float)
    or_low = df["or_low"].to_numpy(dtype=float)
    pnl = np.full(len(events), np.nan, dtype=float)
    hold = np.full(len(events), np.nan, dtype=float)
    reason = np.array(["timeout"] * len(events), dtype=object)
    for e, start in enumerate(idx_arr):
        end = min(start + max_steps, len(df) - 1)
        exit_idx = end
        exit_reason = "timeout"
        for pos in range(start + 1, end + 1):
            if days[pos] != days[start]:
                exit_idx = pos - 1
                exit_reason = "day_end"
                break
            if dir_arr[e] > 0 and mids[pos] < or_high[pos]:
                exit_idx = pos
                exit_reason = "or_reentry"
                break
            if dir_arr[e] < 0 and mids[pos] > or_low[pos]:
                exit_idx = pos
                exit_reason = "or_reentry"
                break
        pnl[e] = dir_arr[e] * (mids[exit_idx] - entry[e])
        hold[e] = max(0, exit_idx - start) * 5.0
        reason[e] = exit_reason
    return ExitResult(pnl=pnl, hold_s=hold, reason=reason)


def make_events(df: pd.DataFrame, mask: pd.Series, direction: np.ndarray, label: str) -> pd.DataFrame:
    emask = mask & (direction != 0)
    out = df.loc[emask, ["idx", "day", "split", "sec", "hour_tpe", "mid_agg", "signC60"]].copy()
    out = out.rename(columns={"mid_agg": "entry_mid"})
    out["direction"] = direction[emask.to_numpy()]
    out["label"] = label
    return out.reset_index(drop=True)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    df = load_wide()
    df = add_pdq_features(df)
    df = add_completed_bar_indicators(df)
    df = add_opening_range(df)
    base_opportunity = build_opportunity_mask(df)
    opportunity = density_match_prior_pdq_cont(df, base_opportunity)
    df["pdq_cont_q95_rebuilt_base"] = base_opportunity.astype(np.int8)
    df["pdq_cont_q95_density_matched"] = opportunity.astype(np.int8)
    mids, future, _same = path_arrays(df, max_horizon_s=900)

    sign_c = df["signC60"].to_numpy(dtype=np.int8)
    dir_tsi = df["dir_tsi15"].fillna(0).to_numpy(dtype=np.int8)
    proxy_dirs = {
        "BASE_signC": sign_c,
        "TSI15_align": np.where((dir_tsi != 0) & (dir_tsi == sign_c), dir_tsi, 0).astype(np.int8),
        "Ret15m_align": np.where(
            (df["dir_ret15"].to_numpy(dtype=np.int8) == sign_c), df["dir_ret15"], 0
        ).astype(np.int8),
        "EMA15Slope_align": np.where(
            (df["dir_ema15"].fillna(0).to_numpy(dtype=np.int8) == sign_c),
            df["dir_ema15"].fillna(0),
            0,
        ).astype(np.int8),
        "CSlow15_align": np.where(
            (df["dir_cslow15"].to_numpy(dtype=np.int8) == sign_c), df["dir_cslow15"], 0
        ).astype(np.int8),
        "DirEff15_align": np.where(
            (df["dir_deff15"].to_numpy(dtype=np.int8) == sign_c), df["dir_deff15"], 0
        ).astype(np.int8),
    }
    deff_abs_q70 = df.groupby("split")["deff15"].transform(lambda s: s.abs().quantile(0.70))
    proxy_dirs["DirEff15_q70_align"] = np.where(
        (df["dir_deff15"].to_numpy(dtype=np.int8) == sign_c)
        & (df["deff15"].abs().to_numpy(dtype=float) > deff_abs_q70.to_numpy(dtype=float)),
        df["dir_deff15"].to_numpy(dtype=np.int8),
        0,
    ).astype(np.int8)

    all_events: list[pd.DataFrame] = []
    for label, direction in proxy_dirs.items():
        events = make_events(df, opportunity, direction, label)
        events = add_path_metrics(events, future)
        all_events.append(events)
    events_all = pd.concat(all_events, ignore_index=True)

    # Test 1: TSI15 vs slow-trend proxy variables.
    rows = []
    for label, group in events_all.groupby("label", sort=True):
        for split, sub in group.groupby("split", sort=True):
            row = {
                "test": "T1_slow_proxy",
                "signal": label,
                "split": split,
                "exit": "fixed300",
            }
            row.update(summarize_events(sub, "pnl_fixed_300", "mfe_300"))
            rows.append(row)
    test1 = pd.DataFrame(rows)
    test1.to_csv(OUT_DIR / "test1_slow_proxy_comparison.csv", index=False)

    # Test 2: TSI15 and high-frequency C direction interaction.
    opp_df = df.loc[opportunity].copy()
    opp_dir = np.zeros(len(opp_df), dtype=np.int8)
    tsi_opp = opp_df["dir_tsi15"].fillna(0).to_numpy(dtype=np.int8)
    c_opp = opp_df["signC60"].to_numpy(dtype=np.int8)
    states = np.full(len(opp_df), "TSI_no_dir_C_dir", dtype=object)
    states[(tsi_opp != 0) & (tsi_opp == c_opp)] = "TSI_C_aligned"
    states[(tsi_opp != 0) & (tsi_opp == -c_opp)] = "TSI_C_opposite"
    opp_df["tsi_c_state"] = states
    opp_signc = make_events(df, opportunity, sign_c, "TSI_C_state_signC")
    opp_signc["tsi_c_state"] = states
    opp_signc = add_path_metrics(opp_signc, future)
    opp_tsi_dir = np.where(dir_tsi != 0, dir_tsi, 0).astype(np.int8)
    opp_tsi = make_events(df, opportunity, opp_tsi_dir, "TSI_C_state_TSI_dir")
    opp_tsi["tsi_c_state"] = opp_df.loc[opp_tsi["idx"].to_numpy(), "tsi_c_state"].to_numpy()
    opp_tsi = add_path_metrics(opp_tsi, future)
    rows = []
    for basis, group in (("trade_signC", opp_signc), ("trade_TSI_dir", opp_tsi)):
        for (split, state), sub in group.groupby(["split", "tsi_c_state"], sort=True):
            row = {
                "test": "T2_tsi_c_direction_interaction",
                "basis": basis,
                "split": split,
                "tsi_c_state": state,
            }
            row.update(summarize_events(sub, "pnl_fixed_300", "mfe_300"))
            rows.append(row)
    test2 = pd.DataFrame(rows)
    test2.to_csv(OUT_DIR / "test2_tsi_c_direction_interaction.csv", index=False)

    # Test 3: hour distribution for the TSI15-aligned candidate.
    tsi_events = events_all[events_all["label"] == "TSI15_align"].copy()
    rows = []
    for (split, hour), sub in tsi_events.groupby(["split", "hour_tpe"], sort=True):
        row = {"test": "T3_hour_distribution", "split": split, "hour_tpe": int(hour)}
        row.update(summarize_events(sub, "pnl_fixed_300", "mfe_300"))
        rows.append(row)
    pd.DataFrame(rows).to_csv(OUT_DIR / "test3_tsi15_by_hour.csv", index=False)

    # Test 4: day contribution and leave-top-days robustness.
    day_rows = []
    for split, sub in tsi_events.groupby("split", sort=True):
        daily = (
            sub.groupby("day", sort=True)
            .agg(
                n=("idx", "size"),
                pnl_fixed300=("pnl_fixed_300", "sum"),
                pnl_mean_fixed300=("pnl_fixed_300", "mean"),
                mfe_ge8_rate=("mfe_300", lambda s: float((s >= 8.0).mean())),
            )
            .reset_index()
        )
        daily["split"] = split
        day_rows.append(daily)
    day_contrib = pd.concat(day_rows, ignore_index=True)
    day_contrib.to_csv(OUT_DIR / "test4_tsi15_day_contribution.csv", index=False)
    test4_rows = []
    for split, sub in tsi_events.groupby("split", sort=True):
        row = {"test": "T4_day_contribution", "split": split}
        row.update(summarize_events(sub, "pnl_fixed_300", "mfe_300"))
        test4_rows.append(row)
    pd.DataFrame(test4_rows).to_csv(OUT_DIR / "test4_tsi15_day_robustness_summary.csv", index=False)

    # Test 5: fixed holds and signal invalidation exits.
    rows = []
    for hold in (180, 300, 600, 900):
        for split, sub in tsi_events.groupby("split", sort=True):
            row = {
                "test": "T5_fixed_hold_vs_invalidation",
                "signal": "TSI15_align",
                "split": split,
                "exit": f"fixed{hold}",
            }
            row.update(summarize_events(sub, f"pnl_fixed_{hold}", f"mfe_{hold}"))
            rows.append(row)
    inv_variants = {
        "invalidate_C_only_max900": ("signC60",),
        "invalidate_TSI_only_max900": ("dir_tsi15",),
        "invalidate_C_or_TSI_max900": ("signC60", "dir_tsi15"),
    }
    for exit_name, cols in inv_variants.items():
        res = signal_invalidation_exit(df, tsi_events, "direction", maxhold_s=900, require_cols=cols)
        tmp = tsi_events.copy()
        tmp["pnl_invalidation"] = res.pnl
        tmp["hold_invalidation"] = res.hold_s
        tmp["exit_reason"] = res.reason
        tmp = add_path_metrics(tmp.drop(columns=[c for c in tmp.columns if c.startswith("pnl_fixed_") or c.startswith("mfe_") or c.startswith("mae_")], errors="ignore"), future)
        for split, sub in tmp.groupby("split", sort=True):
            row = {
                "test": "T5_fixed_hold_vs_invalidation",
                "signal": "TSI15_align",
                "split": split,
                "exit": exit_name,
            }
            row.update(summarize_events(sub, "pnl_invalidation", "mfe_900", "hold_invalidation"))
            rows.append(row)
    pd.DataFrame(rows).to_csv(OUT_DIR / "test5_fixed_hold_vs_signal_invalidation.csv", index=False)

    # Separate Opening Range branch: OR as direction prior, no TSI merge.
    dir_or = df["dir_or"].fillna(0).to_numpy(dtype=np.int8)
    dir_or_align = np.where((dir_or != 0) & (dir_or == sign_c), dir_or, 0).astype(np.int8)
    dir_or_fisher = np.where(
        (dir_or_align != 0)
        & (df["dir_fisher3"].fillna(0).to_numpy(dtype=np.int8) == dir_or_align),
        dir_or_align,
        0,
    ).astype(np.int8)
    or_events = []
    for label, direction in {
        "OR_align": dir_or_align,
        "OR_align_Fisher3m": dir_or_fisher,
    }.items():
        ev = make_events(df, opportunity, direction, label)
        ev = add_path_metrics(ev, future)
        inv = or_invalidation_exit(df, ev, maxhold_s=900)
        ev["pnl_or_invalidation"] = inv.pnl
        ev["hold_or_invalidation"] = inv.hold_s
        ev["or_exit_reason"] = inv.reason
        or_events.append(ev)
    or_all = pd.concat(or_events, ignore_index=True)
    rows = []
    for label, group in or_all.groupby("label", sort=True):
        for exit_name, pnl_col, mfe_col, hold_col in [
            ("fixed300", "pnl_fixed_300", "mfe_300", None),
            ("fixed600", "pnl_fixed_600", "mfe_600", None),
            ("fixed900", "pnl_fixed_900", "mfe_900", None),
            ("or_range_invalidation_max900", "pnl_or_invalidation", "mfe_900", "hold_or_invalidation"),
        ]:
            for split, sub in group.groupby("split", sort=True):
                row = {
                    "test": "OR_separate_branch",
                    "signal": label,
                    "split": split,
                    "exit": exit_name,
                }
                row.update(summarize_events(sub, pnl_col, mfe_col, hold_col))
                rows.append(row)
    pd.DataFrame(rows).to_csv(OUT_DIR / "opening_range_separate_branch.csv", index=False)

    events_all.to_csv(OUT_DIR / "event_level_proxy_paths.csv.gz", index=False, compression="gzip")
    meta = {
        "source": str(DATA_PATH.relative_to(ROOT)),
        "output_dir": str(OUT_DIR.relative_to(ROOT)),
        "is_period": "2026-03-03..2026-04-30",
        "oos_period": "2026-05-01..2026-06-13",
        "rows_common": int(len(df)),
        "pdq_cont_q95_events": {
            split: int(opportunity[df["split"] == split].sum()) for split in ("IS", "OOS")
        },
        "pdq_cont_q95_rebuilt_base_events": {
            split: int(base_opportunity[df["split"] == split].sum()) for split in ("IS", "OOS")
        },
        "density_match_prior_counts": PRIOR_LF_BASELINE_COUNTS,
        "density_match_score": "abs(C60) * rvexp within rebuilt candidate set",
        "price_unit": "TAIFEX points",
        "bar_alignment": "TSI15/Fisher3m use previous completed bar only",
        "costs_points": list(COSTS),
        "notes": [
            "Research-only audit; no live strategy integration.",
            "PDQ_cont q95 definition is fixed inside this script for decomposition tests.",
            "No additional parameter grid beyond requested fixed holds and signal invalidation exits.",
        ],
    }
    (OUT_DIR / "metadata.json").write_text(json.dumps(meta, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
