"""Causal Python port of the Zeiierman 'Machine Learning RSI' Pine v6 indicator.

Mirrors the published default parameters and per-bar semantics: the 8-feature
RSI engine, the horizon-delayed labeled analog bank, Fisher auto-weights, the
Lorentzian distance-weighted KNN vote, the ML adaptive Supertrend trailing
stop, the vol/chop/trend gates, the rank/confidence scores, and the
trigger_long / trigger_short signal series.

Everything is strictly causal: bar i only reads information available at the
close of bar i. No parameter tuning is done anywhere -- the published defaults
are used verbatim so this is a faithful "try the strategy" test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# --- published Pine defaults (verbatim) ------------------------------------
RSI_BASE = 14
MEMORY_DEPTH = 500
K_NEIGHBORS = 8
WIN_LEN = 100
SPACING_BARS = 4
HORIZON_BARS = 4
GATE_RANK = 60.0
GATE_CONF = 50.0
USE_TREND_GATE = True
USE_VOL_BAND = True
VOL_BAND_LO = 20.0
VOL_BAND_HI = 85.0
USE_CHOP = True
ATR_FACTOR = 0.5
TREND_LEN = 50
CHOP_CUT = 0.5
AUTO_WEIGHTS_ON = True
AUTO_SPEED = 1.0
AUTO_FLOOR = 0.5
AUTO_MIN_ROWS = 60
ST_MULT_BASE = 1.5
ST_ML_RESP = 1.0
ST_ATR_LEN = 10
SMOOTH_LEN = 10
COOL_BARS = 5
STEP_LEN = 3
N_FEATURES = 8


# --- causal rolling helpers (offline, numpy) -------------------------------
def _rma(x: np.ndarray, n: int) -> np.ndarray:
    """Wilder's RMA: seed with SMA(n), then recursive (prev*(n-1)+x)/n."""
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = float(np.mean(x[:n]))
    for i in range(n, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def _rsi(src: np.ndarray, n: int) -> np.ndarray:
    d = np.diff(src, prepend=src[0])
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    ru = _rma(up, n)
    rd = _rma(dn, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ru / rd
        rsi = 100.0 - 100.0 / (1.0 + rs)
    rsi = np.where(rd == 0, 100.0, rsi)  # all-gains window -> RSI 100
    rsi = np.where(np.isnan(ru), np.nan, rsi)
    return rsi


def _ema(src: np.ndarray, n: int) -> np.ndarray:
    a = 2.0 / (n + 1.0)
    out = np.full(len(src), np.nan)
    prev = np.nan
    for i in range(len(src)):
        v = src[i]
        if np.isnan(v):
            out[i] = prev
            continue
        prev = v if np.isnan(prev) else a * v + (1.0 - a) * prev
        out[i] = prev
    return out


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    return _rma(tr, n)


def _sma(src: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(src), np.nan)
    csum = np.cumsum(np.nan_to_num(src, nan=0.0))
    for i in range(n - 1, len(src)):
        window = src[i - n + 1 : i + 1]
        if np.any(np.isnan(window)):
            continue
        out[i] = (csum[i] - (csum[i - n] if i >= n else 0.0)) / n
    return out


def _stdev(src: np.ndarray, n: int) -> np.ndarray:
    """Population stdev over a trailing window of n (Pine biased default)."""
    out = np.full(len(src), np.nan)
    for i in range(n - 1, len(src)):
        window = src[i - n + 1 : i + 1]
        if np.any(np.isnan(window)):
            continue
        out[i] = float(np.std(window))
    return out


def _roll_minmax(src: np.ndarray, n: int, want_max: bool) -> np.ndarray:
    """Rolling lowest/highest over the last n bars (inclusive of current)."""
    out = np.full(len(src), np.nan)
    for i in range(len(src)):
        lo = max(0, i - n + 1)
        window = src[lo : i + 1]
        valid = window[~np.isnan(window)]
        if valid.size == 0:
            continue
        out[i] = float(valid.max() if want_max else valid.min())
    return out


def _scale01(src: np.ndarray, n: int) -> np.ndarray:
    lo = _roll_minmax(src, n, want_max=False)
    hi = _roll_minmax(src, n, want_max=True)
    out = np.full(len(src), np.nan)
    for i in range(len(src)):
        if np.isnan(src[i]) or np.isnan(lo[i]) or np.isnan(hi[i]):
            continue
        out[i] = 0.5 if hi[i] == lo[i] else (src[i] - lo[i]) / (hi[i] - lo[i])
    return out


def _percentrank(src: np.ndarray, n: int) -> np.ndarray:
    """Pine ta.percentrank: % of the previous n values strictly less than current."""
    out = np.full(len(src), np.nan)
    for i in range(n, len(src)):
        cur = src[i]
        prev = src[i - n : i]
        if np.isnan(cur) or np.any(np.isnan(prev)):
            continue
        out[i] = float(np.count_nonzero(prev < cur)) * 100.0 / n
    return out


def _shift(src: np.ndarray, k: int) -> np.ndarray:
    out = np.full(len(src), np.nan)
    if k < len(src):
        out[k:] = src[: len(src) - k]
    return out


def _compress(d: float) -> float:
    return float(np.log(1.0 + abs(d)))


# --- result container ------------------------------------------------------
@dataclass
class IndicatorResult:
    trigger_long: np.ndarray
    trigger_short: np.ndarray
    bias_dir: np.ndarray
    st_dir: np.ndarray
    rank: np.ndarray
    conf: np.ndarray
    stance: np.ndarray
    diag: dict = field(default_factory=dict)


def compute_indicator(  # noqa: C901 — faithful 1:1 port of the Pine per-bar loop; splitting obscures correspondence
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    *,
    price_src: np.ndarray | None = None,
) -> IndicatorResult:
    """Run the full ML-RSI engine over a continuous bar series. Returns the
    causal trigger / state arrays used by the backtest driver."""
    n = len(close)
    src = close.astype(float) if price_src is None else price_src.astype(float)
    hl2 = (high + low) / 2.0

    # --- RSI feature engine ----------------------------------------------
    rsi = _rsi(src, RSI_BASE)
    rsi_f = _rsi(src, max(2, round(RSI_BASE / 2)))
    rsi_s = _rsi(src, RSI_BASE * 2)
    atr14 = _atr(high, low, close, 14)

    slope_raw = rsi - _shift(rsi, STEP_LEN)
    accel_raw = slope_raw - _shift(slope_raw, STEP_LEN)
    f_value = rsi / 100.0
    f_slope = _scale01(slope_raw, WIN_LEN)
    f_accel = _scale01(accel_raw, WIN_LEN)
    f_mid = np.abs(rsi - 50.0) / 50.0
    f_pct = _percentrank(rsi, WIN_LEN) / 100.0
    f_churn = _scale01(_stdev(rsi, 14), WIN_LEN)
    f_spread = _scale01(rsi_f - rsi_s, WIN_LEN)
    f_regime = _scale01(_ema(rsi, 20) - 50.0, WIN_LEN)
    feats = np.column_stack([f_value, f_slope, f_accel, f_mid, f_pct, f_churn, f_spread, f_regime])

    # --- forward outcome labels (horizon delayed) ------------------------
    move_fwd = src - _shift(src, HORIZON_BARS)
    band_fwd = ATR_FACTOR * _shift(atr14, HORIZON_BARS)
    outcome = np.zeros(n)
    for i in range(n):
        mv, bd = move_fwd[i], band_fwd[i]
        if np.isnan(mv) or np.isnan(bd):
            outcome[i] = 0.0
            continue
        if mv > 2 * bd:
            outcome[i] = 3
        elif mv > bd:
            outcome[i] = 2
        elif mv > 0:
            outcome[i] = 1
        elif mv < -2 * bd:
            outcome[i] = -3
        elif mv < -bd:
            outcome[i] = -2
        elif mv < 0:
            outcome[i] = -1

    # --- context precomputes ---------------------------------------------
    atr_pct = _percentrank(atr14, 100)
    ema_quick = _ema(close, 5)
    ema_trend = _ema(close, TREND_LEN)
    with np.errstate(divide="ignore", invalid="ignore"):
        trend_force = np.where(atr14 > 0, np.abs(ema_quick - ema_trend) / atr14, 0.0)
    chop_raw = trend_force < CHOP_CUT
    osc_reg = _ema(rsi, 20)
    ema_rsi5 = _ema(rsi, 5)
    slope_up = rsi > _shift(rsi, STEP_LEN)
    st_atr = _atr(high, low, close, ST_ATR_LEN)

    # --- per-bar state ----------------------------------------------------
    trigger_long = np.zeros(n, dtype=bool)
    trigger_short = np.zeros(n, dtype=bool)
    bias_dir_arr = np.zeros(n, dtype=int)
    st_dir_arr = np.zeros(n, dtype=int)
    rank_arr = np.zeros(n)
    conf_arr = np.zeros(n)
    stance_arr = np.zeros(n, dtype=int)

    bank: list[np.ndarray] = []  # newest-first; each row = [8 feats, outcome]
    w_auto = np.ones(N_FEATURES)
    conv_smoothed = np.nan  # EMA(convInst, SMOOTH_LEN) running state
    st_long = np.nan
    st_short = np.nan
    st_dir = 1
    stance_state = 0
    stance_age = 0
    stance_changed_hist: list[bool] = []
    last_entry_bar = None

    a_smooth = 2.0 / (SMOOTH_LEN + 1.0)

    for i in range(n):
        prev_close = close[i - 1] if i > 0 else np.nan

        # 1) bank update: row{feat[i-HORIZON], outcome[i]} (Pine order: before scan)
        if i > HORIZON_BARS:
            fp = feats[i - HORIZON_BARS]
            if not np.any(np.isnan(fp)):
                row = np.empty(N_FEATURES + 1)
                row[:N_FEATURES] = fp
                row[N_FEATURES] = outcome[i]
                bank.insert(0, row)
                if len(bank) > MEMORY_DEPTH:
                    bank.pop()

        # 2) auto Fisher weights over the bank
        if AUTO_WEIGHTS_ON and len(bank) >= AUTO_MIN_ROWS:
            w_raw = _auto_weights(bank)
            w_auto = w_auto + AUTO_SPEED * (w_raw - w_auto)
        w = w_auto if AUTO_WEIGHTS_ON else np.ones(N_FEATURES)
        w_sum = float(np.sum(w))

        # 3) KNN neighbor scan (every SPACING_BARS-th bank row) vs current feats
        cur = feats[i]
        bias_dir = 0
        agree_frac = 0.0
        gap_tight = 0.0
        analog_score = 0.0
        k_count = 0
        if len(bank) > 1 and not np.any(np.isnan(cur)):
            gaps: list[float] = []
            classes: list[int] = []
            scan_end = min(MEMORY_DEPTH - 1, len(bank) - 1)
            for idx in range(0, scan_end + 1):
                if idx % SPACING_BARS != 0:
                    continue
                rrow = bank[idx]
                diff = cur - rrow[:N_FEATURES]
                g = float(np.sum(w * np.log1p(np.abs(diff))))
                if np.isnan(g):
                    continue
                gaps.append(g)
                classes.append(int(rrow[N_FEATURES]))
            if gaps:
                order = np.argsort(gaps)[:K_NEIGHBORS]
                sel_gaps = np.array([gaps[j] for j in order])
                sel_cls = np.array([classes[j] for j in order])
                vote_w = 1.0 / (1.0 + sel_gaps)
                total = float(np.sum(vote_w))
                score = float(np.sum(sel_cls * vote_w))
                bull = float(np.sum(vote_w[sel_cls > 0]))
                bear = float(np.sum(vote_w[sel_cls < 0]))
                k_count = len(order)
                analog_score = score / total if total > 0 else 0.0
                bias_dir = 1 if analog_score > 0.15 else (-1 if analog_score < -0.15 else 0)
                if total > 0:
                    agree_frac = (bull if bias_dir == 1 else bear if bias_dir == -1 else 0.0) / total
                avg_gap = float(np.mean(sel_gaps))
                gap_scale = w_sum * 0.45 + 1e-9
                gap_tight = max(0.0, min(1.0, 1.0 - avg_gap / gap_scale))

        # 4) ML adaptive Supertrend
        conv_inst = max(-1.0, min(1.0, analog_score / 1.5))
        conv_smoothed = conv_inst if np.isnan(conv_smoothed) else a_smooth * conv_inst + (1 - a_smooth) * conv_smoothed
        chop_now = bool(chop_raw[i]) if USE_CHOP else False
        ml_drive = max(0.0, min(1.0, abs(conv_smoothed) * 0.5 + gap_tight * 0.3 + agree_frac * 0.2))
        ml_drive = ml_drive * 0.35 if chop_now else ml_drive
        adapt_mult = ST_MULT_BASE * (1.0 + ST_ML_RESP * (1.0 - ml_drive))
        cur_atr = st_atr[i] if not np.isnan(st_atr[i]) else 0.0
        up_band = hl2[i] - adapt_mult * cur_atr
        dn_band = hl2[i] + adapt_mult * cur_atr
        if np.isnan(st_long):
            st_long = up_band
        else:
            st_long = max(up_band, st_long) if (not np.isnan(prev_close) and prev_close > st_long) else up_band
        if np.isnan(st_short):
            st_short = dn_band
        else:
            st_short = min(dn_band, st_short) if (not np.isnan(prev_close) and prev_close < st_short) else dn_band
        prev_dir = st_dir
        if prev_dir == -1 and close[i] > st_short:
            st_dir = 1
        elif prev_dir == 1 and close[i] < st_long:
            st_dir = -1
        # else unchanged
        st_dir_arr[i] = st_dir

        # 5) context
        up_trend = st_dir == 1
        down_trend = st_dir == -1
        vol_healthy = (not np.isnan(atr_pct[i])) and (VOL_BAND_LO <= atr_pct[i] <= VOL_BAND_HI)
        oreg = osc_reg[i]
        s_up = bool(slope_up[i])
        slope_fit = (bias_dir == 1 and s_up) or (bias_dir == -1 and not s_up)
        rv = rsi[i]
        stretched = (bias_dir == 1 and rv > 70) or (bias_dir == -1 and rv < 30)
        osc_smooth_up = (i > 0) and (not np.isnan(ema_rsi5[i])) and (not np.isnan(ema_rsi5[i - 1])) and (
            ema_rsi5[i] > ema_rsi5[i - 1]
        )
        aligned = (bias_dir == 1 and up_trend) or (bias_dir == -1 and down_trend)

        # 6) stance persistence
        gates_pass = (
            (not USE_TREND_GATE or aligned)
            and (not USE_VOL_BAND or vol_healthy)
            and not chop_now
        )
        new_stance = bias_dir if (bias_dir != 0 and gates_pass) else stance_state
        changed = new_stance != stance_state
        stance_state = new_stance
        stance_age = 0 if changed else stance_age + 1
        recent3 = stance_changed_hist[-3:]
        early_flip = changed and any(recent3)
        stance_changed_hist.append(changed)

        # 7) rank / confidence
        rank = _rank_score(bias_dir, agree_frac, gap_tight, slope_fit, stretched, aligned,
                           vol_healthy, atr_pct[i], oreg, osc_smooth_up, stance_age,
                           bool(chop_raw[i]), early_flip, k_count)
        conf = _conf_score(bias_dir, agree_frac, gap_tight, stance_age, slope_fit, early_flip, k_count)

        # 8) signals
        flip_long = stance_state == 1 and stance_arr[i - 1] != 1 if i > 0 else stance_state == 1
        flip_short = stance_state == -1 and stance_arr[i - 1] != -1 if i > 0 else stance_state == -1
        qualifies = rank >= GATE_RANK and conf >= GATE_CONF
        cool_ok = last_entry_bar is None or (i - last_entry_bar) >= COOL_BARS
        t_long = flip_long and qualifies and cool_ok
        t_short = flip_short and qualifies and cool_ok
        if t_long or t_short:
            last_entry_bar = i

        trigger_long[i] = t_long
        trigger_short[i] = t_short
        bias_dir_arr[i] = bias_dir
        rank_arr[i] = rank
        conf_arr[i] = conf
        stance_arr[i] = stance_state

    return IndicatorResult(
        trigger_long=trigger_long,
        trigger_short=trigger_short,
        bias_dir=bias_dir_arr,
        st_dir=st_dir_arr,
        rank=rank_arr,
        conf=conf_arr,
        stance=stance_arr,
        diag={"n_bars": n, "n_long": int(trigger_long.sum()), "n_short": int(trigger_short.sum())},
    )


def _auto_weights(bank: list[np.ndarray]) -> np.ndarray:
    """Fisher discriminant feature importance (bull vs bear), scaled [floor,10]."""
    arr = np.array(bank)
    out = arr[:, N_FEATURES]
    bull_mask = out > 0
    bear_mask = out < 0
    nb = int(bull_mask.sum())
    nbe = int(bear_mask.sum())
    imp = np.ones(N_FEATURES)
    if nb <= 2 or nbe <= 2:
        return imp
    fb = arr[bull_mask, :N_FEATURES]
    fe = arr[bear_mask, :N_FEATURES]
    mb = fb.mean(axis=0)
    mbe = fe.mean(axis=0)
    vb = np.maximum(0.0, fb.var(axis=0))
    vbe = np.maximum(0.0, fe.var(axis=0))
    fish = (mb - mbe) ** 2 / (vb + vbe + 1e-6)
    max_f = float(fish.max())
    if max_f > 0:
        norm = fish / max_f
        imp = np.maximum(AUTO_FLOOR, norm * 10.0)
    return imp


def _rank_score(bias_dir, agree_frac, gap_tight, slope_fit, stretched, aligned,
                vol_healthy, atr_pct, osc_reg, osc_smooth_up, age, chop_raw,
                early_flip, k_count) -> float:
    if bias_dir == 0:
        return 0.0
    p_agree = 25.0 * agree_frac
    p_gap = 15.0 * gap_tight
    p_struct = (10.0 if slope_fit else 0.0) + (0.0 if stretched else 5.0)
    p_trend = 10.0 if aligned else 0.0
    if vol_healthy:
        p_vol = 10.0
    elif not np.isnan(atr_pct) and atr_pct < VOL_BAND_LO:
        p_vol = 5.0
    else:
        p_vol = 3.0
    reg_fit = (bias_dir == 1 and osc_reg > 55) or (bias_dir == -1 and osc_reg < 45)
    if reg_fit:
        p_reg = 10.0
    elif 45 <= osc_reg <= 55:
        p_reg = 4.0
    else:
        p_reg = 6.0
    p_smooth = 5.0 if ((bias_dir == 1 and osc_smooth_up) or (bias_dir == -1 and not osc_smooth_up)) else 0.0
    p_hold = min(5.0, age)
    p_pen = min(20.0, (8.0 if chop_raw else 0.0) + (6.0 if stretched else 0.0) + (6.0 if early_flip else 0.0)
               + (5.0 * (K_NEIGHBORS - k_count) / K_NEIGHBORS if k_count < K_NEIGHBORS else 0.0))
    raw = p_agree + p_gap + p_struct + p_trend + p_vol + p_reg + p_smooth + p_hold - p_pen
    return max(0.0, min(100.0, raw))


def _conf_score(bias_dir, agree_frac, gap_tight, age, slope_fit, early_flip, k_count) -> float:
    if bias_dir == 0:
        return 0.0
    raw = (
        40.0 * agree_frac
        + 25.0 * gap_tight
        + 15.0 * min(1.0, age / 5.0)
        + 10.0 * (1.0 if slope_fit else 0.0)
        - (15.0 if early_flip else 0.0)
        - (10.0 * (K_NEIGHBORS - k_count) / K_NEIGHBORS if k_count < K_NEIGHBORS else 0.0)
    )
    return max(0.0, min(100.0, raw))
