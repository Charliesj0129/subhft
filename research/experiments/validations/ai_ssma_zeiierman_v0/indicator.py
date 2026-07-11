"""Causal Python port of the Zeiierman 'AI Source Switching Moving Average' (v6).

Mirrors the published default parameters and per-bar semantics: the 6-feature
engine evaluated on each of Open/High/Low/Close, the horizon-delayed labeled
analog banks (per-source + a pooled bank for Fisher), Fisher auto-weights, the
Lorentzian distance-weighted KNN vote per source, the online Adam-trained neural
score, the per-source rank that selects which OHLC feeds the line, and the AI
adaptive Supertrend whose flips are the directional signal.

Everything is strictly causal: bar i only reads information available at the
close of bar i. No parameter tuning -- published defaults are used verbatim so
this is a faithful "try the strategy" test. The driver trades the Supertrend
flips (trigger_long on flip-up, trigger_short on flip-down).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# reuse the vetted causal rolling helpers from the sibling ML-RSI port
from research.experiments.validations.ml_rsi_zeiierman_v0.indicator import (
    _atr,
    _ema,
    _scale01,
    _shift,
    _sma,
    _stdev,
)

# --- published Pine defaults (verbatim) ------------------------------------
MA_LEN = 50
SRC_SMOOTH_LEN = 3
MEMORY_DEPTH = 40
K_NEIGHBORS = 9
HORIZON_BARS = 4
SPACING_BARS = 4
LEARN_ATR_FACTOR = 0.45
USE_NEURAL = True
NEURAL_INFLUENCE = 0.35
LEARN_RATE = 0.01
HUBER_D = 0.02
USE_FISHER = True
FISHER_SPEED = 0.20
FISHER_FLOOR = 0.40
MIN_ROWS = 80
ST_LEN = 10
ST_MULT = 1.7
ST_ADAPT = 0.80
WARMUP_BARS = 120  # Pine guard: bar_index > horizonBars + 120
N_FEATURES = 6

_ADAM_B1 = 0.9
_ADAM_B2 = 0.999
_ADAM_EPS = 1e-8


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(x, hi))


def _norm_score(x: float) -> float:
    return 1.0 / (1.0 + np.exp(-_clamp(x, -8.0, 8.0)))


# --- feature engine (6 features per OHLC source) ---------------------------
def _source_features(
    src: np.ndarray, high: np.ndarray, low: np.ndarray, atr14: np.ndarray
) -> np.ndarray:
    """Return an (n, 6) array: [trend, mean, momentum, vol, range, slope]."""
    n = len(src)
    with np.errstate(divide="ignore", invalid="ignore"):
        fast = _ema(src, 10)
        slow = _ema(src, 34)
        f_trend = np.where(atr14 == 0, 0.0, np.clip((fast - slow) / atr14, -3, 3) / 3.0)

        basis = _sma(src, 30)
        dev = _stdev(src, 30)
        z = np.where(dev == 0, 0.0, (src - basis) / dev)
        f_mean = np.clip(-z, -3, 3) / 3.0

        roc = src / _shift(src, 14) - 1.0
        f_mom = np.clip(roc / 0.05, -3, 3) / 3.0

        f_vol = _scale01(_stdev(src, 20), 100) * 2.0 - 1.0

        rng = high - low
        f_rng = np.where(rng == 0, 0.0, np.clip(((src - low) / rng) * 2.0 - 1.0, -1, 1))

        slope = _shift(src, 3)
        f_slope = np.where(atr14 == 0, 0.0, np.clip((src - slope) / atr14, -3, 3) / 3.0)

    feats = np.column_stack([f_trend, f_mean, f_mom, f_vol, f_rng, f_slope])
    # rows whose history is incomplete stay NaN so the bank/KNN skip them
    incomplete = np.zeros(n, dtype=bool)
    incomplete[:34] = True  # slowest EMA seed
    feats[incomplete] = np.nan
    return feats


def _outcome_labels(close: np.ndarray, atr14: np.ndarray) -> np.ndarray:
    move_fwd = close - _shift(close, HORIZON_BARS)
    band_fwd = LEARN_ATR_FACTOR * _shift(atr14, HORIZON_BARS)
    out = np.zeros(len(close))
    for i in range(len(close)):
        mv, bd = move_fwd[i], band_fwd[i]
        if np.isnan(mv) or np.isnan(bd):
            continue
        if mv > 2 * bd:
            out[i] = 3
        elif mv > bd:
            out[i] = 2
        elif mv > 0:
            out[i] = 1
        elif mv < -2 * bd:
            out[i] = -3
        elif mv < -bd:
            out[i] = -2
        elif mv < 0:
            out[i] = -1
    return out


def _auto_weights(bank_all: list[np.ndarray]) -> np.ndarray:
    """Fisher discriminant feature importance over the pooled bank, scaled
    [floor, 8]. Returns 6 ones until there is enough class-balanced evidence."""
    imp = np.ones(N_FEATURES)
    if len(bank_all) < MIN_ROWS:
        return imp
    arr = np.array(bank_all)
    cls = arr[:, N_FEATURES]
    bull = arr[cls > 0, :N_FEATURES]
    bear = arr[cls < 0, :N_FEATURES]
    if bull.shape[0] <= 3 or bear.shape[0] <= 3:
        return imp
    mb, mbe = bull.mean(axis=0), bear.mean(axis=0)
    vb = np.maximum(0.0, bull.var(axis=0))
    vbe = np.maximum(0.0, bear.var(axis=0))
    fish = (mb - mbe) ** 2 / (vb + vbe + 1e-6)
    max_f = float(fish.max())
    if max_f > 0:
        imp = np.maximum(FISHER_FLOOR, (fish / max_f) * 8.0)
    return imp


def _knn_score(cur: np.ndarray, bank: list[np.ndarray], w: np.ndarray) -> tuple[float, float, float, int]:
    """Lorentzian distance-weighted KNN vote of `cur` against `bank` (newest
    first). Returns (analog, agree_frac, gap_tight, k_count)."""
    if len(bank) <= 1 or np.any(np.isnan(cur)):
        return 0.0, 0.0, 0.0, 0
    scan_end = min(len(bank) - 1, MEMORY_DEPTH - 1)
    gaps: list[float] = []
    classes: list[int] = []
    for idx in range(0, scan_end + 1):
        if idx % SPACING_BARS != 0:
            continue
        row = bank[idx]
        cls = int(row[N_FEATURES])
        if cls == 0:
            continue
        diff = cur - row[:N_FEATURES]
        g = float(np.sum(w * np.log1p(np.abs(diff))))
        if np.isnan(g):
            continue
        gaps.append(g)
        classes.append(cls)
    if not gaps:
        return 0.0, 0.0, 0.0, 0
    order = np.argsort(gaps)[:K_NEIGHBORS]
    sel_g = np.array([gaps[j] for j in order])
    sel_c = np.array([classes[j] for j in order])
    vote_w = 1.0 / (1.0 + sel_g)
    total = float(np.sum(vote_w))
    score = float(np.sum(sel_c * vote_w))
    bull = float(np.sum(vote_w[sel_c > 0]))
    bear = float(np.sum(vote_w[sel_c < 0]))
    analog = score / total if total > 0 else 0.0
    direction = 1 if analog > 0.15 else (-1 if analog < -0.15 else 0)
    agree = (bull if direction == 1 else bear if direction == -1 else 0.0) / total if total > 0 else 0.0
    avg_gap = float(np.mean(sel_g))
    gap_scale = float(np.sum(w)) * 0.45 + 1e-9
    tight = _clamp(1.0 - avg_gap / gap_scale, 0.0, 1.0)
    return analog, agree, tight, len(order)


@dataclass
class IndicatorResult:
    trigger_long: np.ndarray   # Supertrend flip up
    trigger_short: np.ndarray  # Supertrend flip down
    st_dir: np.ndarray
    best_src: np.ndarray       # 0=O 1=H 2=L 3=C selected source per bar
    ai_ma: np.ndarray
    diag: dict = field(default_factory=dict)


def compute_indicator(  # noqa: C901 — faithful 1:1 port of the Pine per-bar loop
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    **_ignored,
) -> IndicatorResult:
    """Run the AI Source Switching engine over a continuous bar series and
    return the causal Supertrend-flip signals used by the backtest driver."""
    n = len(close)
    open_ = open_.astype(float)
    high = high.astype(float)
    low = low.astype(float)
    close = close.astype(float)
    atr14 = _atr(high, low, close, 14)
    st_atr = _atr(high, low, close, ST_LEN)

    # per-source feature arrays (n, 6) for O/H/L/C
    src_arrays = [open_, high, low, close]
    feats = [_source_features(s, high, low, atr14) for s in src_arrays]  # 4 x (n,6)
    feats_close = feats[3]
    outcome = _outcome_labels(close, atr14)

    trigger_long = np.zeros(n, dtype=bool)
    trigger_short = np.zeros(n, dtype=bool)
    st_dir_arr = np.zeros(n, dtype=int)
    best_src_arr = np.zeros(n, dtype=int)

    banks: list[list[np.ndarray]] = [[], [], [], []]  # per-source, newest first
    bank_all: list[np.ndarray] = []
    w_auto = np.ones(N_FEATURES)

    # neural Adam state (6 weights + bias)
    nw = np.full(N_FEATURES, 0.01)
    nb = 0.0
    m_w = np.zeros(N_FEATURES)
    v_w = np.zeros(N_FEATURES)
    m_b = 0.0
    v_b = 0.0
    step = 0

    # supertrend state
    st_long = np.nan
    st_short = np.nan
    st_dir = 1
    ai_src_prev = np.nan  # for the EMA(srcSmoothLen) of the selected source
    a_src = 2.0 / (SRC_SMOOTH_LEN + 1.0)
    ai_ma = np.full(n, np.nan)
    a_ma = 2.0 / (MA_LEN + 1.0)
    ai_ma_prev = np.nan

    best_counts = [0, 0, 0, 0]

    for i in range(n):
        prev_close = close[i - 1] if i > 0 else np.nan
        warm = i > HORIZON_BARS + WARMUP_BARS

        # 1) bank update: pair delayed feature[i-HORIZON] with outcome[i]
        if warm:
            for s in range(4):
                fp = feats[s][i - HORIZON_BARS]
                if not np.any(np.isnan(fp)):
                    row = np.empty(N_FEATURES + 1)
                    row[:N_FEATURES] = fp
                    row[N_FEATURES] = outcome[i]
                    banks[s].insert(0, row)
                    if len(banks[s]) > MEMORY_DEPTH:
                        banks[s].pop()
                    bank_all.insert(0, row)
                    if len(bank_all) > MEMORY_DEPTH * 4:
                        bank_all.pop()

        # 2) Fisher auto-weights (EMA toward the pooled-bank estimate each bar)
        if USE_FISHER:
            w_raw = _auto_weights(bank_all)
            w_auto = w_auto + FISHER_SPEED * (w_raw - w_auto)
        w = w_auto if USE_FISHER else np.ones(N_FEATURES)

        # 3) per-source KNN vote
        analog = np.zeros(4)
        agree = np.zeros(4)
        tight = np.zeros(4)
        kcnt = np.zeros(4, dtype=int)
        for s in range(4):
            a_, ag_, t_, k_ = _knn_score(feats[s][i], banks[s], w)
            analog[s], agree[s], tight[s], kcnt[s] = a_, ag_, t_, k_

        # 4) online neural training (Adam) on the close-source delayed features
        target_dir = 1.0 if outcome[i] > 0 else (-1.0 if outcome[i] < 0 else 0.0)
        fc_delayed = feats_close[i - HORIZON_BARS] if i >= HORIZON_BARS else np.full(N_FEATURES, np.nan)
        if USE_NEURAL and warm and target_dir != 0 and not np.any(np.isnan(fc_delayed)):
            pred = float(np.dot(nw, fc_delayed)) + nb
            err = pred - target_dir
            grad_core = err if abs(err) <= HUBER_D else HUBER_D * np.sign(err)
            step += 1
            bc1 = 1.0 - _ADAM_B1**step
            bc2 = 1.0 - _ADAM_B2**step
            g_w = grad_core * fc_delayed
            m_w = _ADAM_B1 * m_w + (1 - _ADAM_B1) * g_w
            v_w = _ADAM_B2 * v_w + (1 - _ADAM_B2) * g_w * g_w
            nw = nw - LEARN_RATE * (m_w / bc1) / (np.sqrt(v_w / bc2) + _ADAM_EPS)
            m_b = _ADAM_B1 * m_b + (1 - _ADAM_B1) * grad_core
            v_b = _ADAM_B2 * v_b + (1 - _ADAM_B2) * grad_core * grad_core
            nb = nb - LEARN_RATE * (m_b / bc1) / (np.sqrt(v_b / bc2) + _ADAM_EPS)

        # 5) per-source rank -> select active OHLC source
        ranks = np.zeros(4)
        for s in range(4):
            fs = feats[s][i]
            neural = float(np.dot(nw, fs)) + nb if (USE_NEURAL and not np.any(np.isnan(fs))) else 0.0
            directional = abs(analog[s]) / 3.0
            raw = (
                directional * 0.35
                + agree[s] * 0.25
                + tight[s] * 0.20
                + _norm_score(neural) * NEURAL_INFLUENCE
                + (0.10 if kcnt[s] >= K_NEIGHBORS else 0.0)
            )
            ranks[s] = _clamp(raw, 0.0, 1.0)
        ready = all(len(banks[s]) > 20 for s in range(4))
        safe = ranks if ready else np.full(4, 0.25)
        # tie order O>=H>=L>=C (argmax returns first max -> matches Pine cascade)
        best_id = int(np.argmax(safe))
        best_src_arr[i] = best_id
        best_counts[best_id] += 1

        hard_src = src_arrays[best_id][i]
        ai_src = hard_src if np.isnan(ai_src_prev) else a_src * hard_src + (1 - a_src) * ai_src_prev
        ai_src_prev = ai_src
        ai_ma_prev = ai_src if np.isnan(ai_ma_prev) else a_ma * ai_src + (1 - a_ma) * ai_ma_prev
        ai_ma[i] = ai_ma_prev

        # 6) AI adaptive Supertrend (bands on the selected, smoothed source)
        avg_analog = float(np.mean(analog))
        avg_agree = float(np.mean(agree))
        avg_tight = float(np.mean(tight))
        ai_drive = _clamp(abs(avg_analog) * 0.20 + avg_agree * 0.40 + avg_tight * 0.40, 0.0, 1.0)
        adapt_mult = ST_MULT * (1.0 + ST_ADAPT * (1.0 - ai_drive))
        cur_atr = st_atr[i] if not np.isnan(st_atr[i]) else 0.0
        up_band = ai_src - adapt_mult * cur_atr
        dn_band = ai_src + adapt_mult * cur_atr
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
        st_dir_arr[i] = st_dir
        if i > 0:
            trigger_long[i] = st_dir == 1 and st_dir_arr[i - 1] == -1
            trigger_short[i] = st_dir == -1 and st_dir_arr[i - 1] == 1

    return IndicatorResult(
        trigger_long=trigger_long,
        trigger_short=trigger_short,
        st_dir=st_dir_arr,
        best_src=best_src_arr,
        ai_ma=ai_ma,
        diag={
            "n_bars": n,
            "n_flip_up": int(trigger_long.sum()),
            "n_flip_down": int(trigger_short.sum()),
            "best_src_counts": {"open": best_counts[0], "high": best_counts[1],
                                "low": best_counts[2], "close": best_counts[3]},
        },
    )
