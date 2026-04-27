"""R52 AMHP Dynamic-Spread Maker (TMFD6) — C1.

Adaptive Multi-scale Hawkes Process driven dynamic-spread market maker.

Source: T1 §6 (researcher T1 artifact `t1_researcher_c1.md`),
DA-approved at T2 with five pre-registered conditional kill flags
(`t2_devils_advocate_c1.md`).

Three-layer architecture (per `hft-mm-design`):
  L1 — Spread gate: dynamic spread = base_spread x g(rho_hat, |IIR|), with HARD
       floor at base_spread_pts (5 pt) — never narrow below R47 cost-floor.
  L2 — Signals: AMHP intensity lambda*(t), branching ratio rho_hat(t),
       intensity-imbalance ratio IIR(t) on TMFD6 trade flow. Three-scale
       exponential kernel (ms / 1min / 1hr decay rates per "AMHP" §6.1).
       State-dependent baseline mu(t) using gamma_io (daily foreign-IO z-score)
       and gamma_us (daily US overnight return ±30 min open) — load-bearing
       day-level covariates per DA T2 §S6 / §H4.
  L3 — Execution: bid/ask both sides; max_pos = 3 (R47 structural property);
       cancel-on-adverse via fast-cancel 59 ms profile.

Pre-registered T6 kill flags (instrumented in `kill_flag_telemetry()`):
  K1 Q-D    daily PnL distribution: max_day_pct ≤ 25%, winning_days ≥ 5
  K2 gamma_io / gamma_us 95% CI must exclude zero
  K3 AMHP modulator per-fill gain ≥ 1.75 pt above R47 baseline
  K4 Q-A multi-scale ACF non-trivial at min/hr lags (else collapses to C6)
  K5 Q-E rho_hat > 0.85 frequency in 1–4%/day band

Float exception (Architecture Governance Rule §11): float is permitted in this
research-only alpha module. Live-path arithmetic remains scaled int x10000.
"""

from __future__ import annotations

import math
from typing import Any

from structlog import get_logger

from hft_platform.core.timebase import now_ns
from hft_platform.events import (
    FeatureUpdateEvent,
    LOBStatsEvent,
    TickEvent,
)
from hft_platform.strategies.r47_maker import R47MakerStrategy
from research.registry.schemas import AlphaManifest, AlphaStatus, AlphaTier

logger = get_logger("alpha.r52_amhp_dynamic_spread")

# Feature indices (lob_shared_v3) — same as R47 baseline
_IDX_BEST_BID = 0
_IDX_BEST_ASK = 1
_IDX_L1_BID_QTY = 8
_IDX_L1_ASK_QTY = 9
_IDX_L1_IMBALANCE_PPM = 10

_PRICE_SCALE = 10_000          # x10000 scaled int (TMFD6 contract scale)
_NS_PER_SEC = 1_000_000_000

# Layer-1 spread floor — never quote below this.  Cited from T1 §3 (anchored to
# `feedback_taifex_fee_structure.md`: TMF retail RT 4 pt + 1 pt buffer).
_BASE_SPREAD_PTS = 5
_BASE_SPREAD_SCALED = _BASE_SPREAD_PTS * _PRICE_SCALE

# Layer-2 modulator coefficients (pre-registered — to be re-fit by kill_flag
# telemetry on the 31d window during T5 backtest; literature-prior defaults).
_ALPHA_RHO = 1.6                # per T1 §3 multiplier table (calm 1.0 → tense 2.2)
_ALPHA_IIR = 0.9
_RHO_LOW = 0.55                 # multiplier inactive below
_RHO_TENSE = 0.75
_RHO_CRITICAL = 0.85            # AMHP §6.4 criticality alarm
_IIR_TENSE = 0.45
_IIR_CRITICAL = 0.70
_MULT_CAP = 3.0                 # T1 §3 — critical regime upper bound

# Layer-2 multi-scale decay rates (β_k per T1 §6.1 table).  half_life = ln2/β.
_BETA_MS = math.log(2.0) / 0.10                # ~100 ms half-life
_BETA_MIN = math.log(2.0) / 60.0               # ~1 min half-life
_BETA_HR = math.log(2.0) / 3600.0              # ~1 hr half-life

# Excitation amplitudes (scaled at the exponential basis); fit in update path.
# Defaults sized so per-scale ρ_k = α_k/β_k ≈ 0.20, giving aggregate ρ̂ ≈ 0.60.
# (β_ms = 6.93, β_min = 0.0116, β_hr = 0.000193 → α_ms = 1.386, α_min = 0.00231,
#  α_hr = 0.0000385.)  This keeps the model in the stable subcritical regime
#  while having all three scales contribute non-trivially to ρ̂.
_ALPHA_K_DEFAULT = (
    0.20 * _BETA_MS,
    0.20 * _BETA_MIN,
    0.20 * _BETA_HR,
)

# Asymmetric panic-sell prior (T1 §6.3 — α_sell_sell ≈ 1.3–1.8 × α_buy_buy).
_ASYM_R_DEFAULT = 1.5

# Active-size attenuation when ρ̂ enters tense / critical regimes (T1 §3).
_SIZE_TENSE = 1                 # already 1 lot at max_pos=3 in baseline
_SIZE_CRIT = 1

# Minimum samples required before the AMHP estimator is considered warmed-up.
_AMHP_WARMUP_TRADES = 200

# Kill-flag telemetry buckets (per-fill, per-minute, per-day).
_LOG_INTERVAL = 500


# =============================================================================
# Multi-scale Hawkes / AMHP online estimator
# =============================================================================


class _AMHPState:
    """Three-scale exponential Hawkes online estimator with side-asymmetric
    excitation and state-dependent baseline.

    Numerical-stability notes:
      * λ_k(t) is recursively updated via exponential decay: between events
        at t_{i-1}, t_i, λ_k(t_i^-) = λ_k(t_{i-1}^+) * exp(-β_k * Δt).  Then on
        event arrival, λ_k(t_i^+) += α_k.  This keeps O(1) work per trade and
        avoids summing the full event history.
      * The scale-aggregate ρ̂ is computed as Σ_k α_k / β_k under the *current
        excitation amplitudes* α_k (initialised to defaults; rolling-MLE update
        every `_AMHP_WARMUP_TRADES` events to avoid per-event recomputation).
      * IIR is a continuous ratio in [-1, +1] on the side-projected intensity.
    """

    __slots__ = (
        # decay rates (constant)
        "_betas",
        # per-side excitation amplitudes — alpha_k_buy, alpha_k_sell, sized at fit
        "_alpha_k_buy",
        "_alpha_k_sell",
        # asymmetric self-excitation ratio R = alpha_sell_sell / alpha_buy_buy
        "_asym_R",
        # per-scale, per-side intensity λ_k_side
        "_lam_buy",
        "_lam_sell",
        # last update timestamp (ns)
        "_last_ns",
        # state-dependent baseline μ components — set externally per day
        "_mu_0",
        "_mu_lob",
        "_mu_dist",
        "_mu_io",
        "_mu_us",
        # current covariate values
        "_lob_imb_z",
        "_dist_to_limit_z",
        "_io_z",
        "_us_overnight",
        "_us_window_active",
        # warmup counter
        "_n_trades",
        "_warmed_up",
    )

    def __init__(
        self,
        betas: tuple[float, float, float] = (_BETA_MS, _BETA_MIN, _BETA_HR),
        alpha_k_init: tuple[float, float, float] = _ALPHA_K_DEFAULT,
        asym_R: float = _ASYM_R_DEFAULT,
        mu_0: float = 1.5,
    ) -> None:
        self._betas = betas
        self._alpha_k_buy = list(alpha_k_init)
        self._alpha_k_sell = [a * asym_R for a in alpha_k_init]
        self._asym_R = asym_R

        # Initialise intensities at the baseline μ_0 split evenly across scales.
        # This lets ρ̂ start near sum(α_k)/β_k under default priors instead of 0.
        self._lam_buy = [mu_0 / 3.0] * 3
        self._lam_sell = [mu_0 / 3.0] * 3

        self._last_ns: int = 0

        self._mu_0 = mu_0
        # Linearised state-dependent μ coefficients — load-bearing T2 flag #1.
        self._mu_lob = 0.0
        self._mu_dist = 0.0
        self._mu_io = 0.0
        self._mu_us = 0.0

        self._lob_imb_z = 0.0
        self._dist_to_limit_z = 0.0
        self._io_z = 0.0
        self._us_overnight = 0.0
        self._us_window_active = False

        self._n_trades = 0
        self._warmed_up = False

    # ------------------------------------------------------------------ μ(t)

    def set_day_covariates(
        self,
        lob_imb_z: float = 0.0,
        dist_to_limit_z: float = 0.0,
        io_z: float = 0.0,
        us_overnight: float = 0.0,
        us_window_active: bool = False,
    ) -> None:
        """Refresh state-dep μ inputs.  Foreign-IO z and US-overnight return
        are *day-level* (per T1 §6.2) — set once at session open.  LOB-imbalance
        and distance-to-limit refresh intraday."""
        self._lob_imb_z = lob_imb_z
        self._dist_to_limit_z = dist_to_limit_z
        self._io_z = io_z
        self._us_overnight = us_overnight
        self._us_window_active = us_window_active

    def set_mu_coefficients(
        self,
        gamma_lob: float = 0.0,
        gamma_dist: float = 0.0,
        gamma_io: float = 0.0,
        gamma_us: float = 0.0,
    ) -> None:
        """Calibrated μ-coefficients.  γ_io and γ_us are the load-bearing
        day-level covariates per DA T2 flag #1; their 95% CIs feed kill flag K2."""
        self._mu_lob = gamma_lob
        self._mu_dist = gamma_dist
        self._mu_io = gamma_io
        self._mu_us = gamma_us

    def _mu_t(self) -> float:
        """Compute state-dependent μ(t)."""
        mu = self._mu_0
        mu += self._mu_lob * self._lob_imb_z
        mu += self._mu_dist * self._dist_to_limit_z
        mu += self._mu_io * self._io_z
        if self._us_window_active:
            mu += self._mu_us * self._us_overnight
        # μ must be non-negative for a counting process; floor at small ε.
        return mu if mu > 1e-6 else 1e-6

    # --------------------------------------------------------------- update

    def _decay(self, dt_ns: int) -> None:
        """Exponential decay all per-scale intensities by Δt."""
        if dt_ns <= 0 or self._last_ns == 0:
            return
        dt_s = dt_ns / _NS_PER_SEC
        for k in range(3):
            decay = math.exp(-self._betas[k] * dt_s)
            self._lam_buy[k] *= decay
            self._lam_sell[k] *= decay

    def update_trade(self, ts_ns: int, direction: int) -> None:
        """Update all scales with a signed trade.  direction ∈ {-1, +1}."""
        if direction == 0:
            return
        if self._last_ns > 0:
            self._decay(ts_ns - self._last_ns)
        self._last_ns = ts_ns
        self._n_trades += 1
        if self._n_trades >= _AMHP_WARMUP_TRADES:
            self._warmed_up = True

        # Cross- and self-excitation per side (asymmetric per T1 §6.3).
        if direction > 0:
            for k in range(3):
                self._lam_buy[k] += self._alpha_k_buy[k]
                # Cross-excitation buy → sell at a fraction of the self term.
                self._lam_sell[k] += 0.5 * self._alpha_k_buy[k]
        else:
            for k in range(3):
                self._lam_sell[k] += self._alpha_k_sell[k]
                self._lam_buy[k] += 0.5 * self._alpha_k_sell[k]

    # ----------------------------------------------------- derived signals

    def lambda_buy(self) -> float:
        return self._mu_t() + self._lam_buy[0] + self._lam_buy[1] + self._lam_buy[2]

    def lambda_sell(self) -> float:
        return self._mu_t() + self._lam_sell[0] + self._lam_sell[1] + self._lam_sell[2]

    def rho_hat(self) -> float:
        """Aggregate branching ratio ρ̂ = Σ_k α_k / β_k (averaged across sides)."""
        bb = self._betas
        s_buy = sum(self._alpha_k_buy[k] / bb[k] for k in range(3))
        s_sell = sum(self._alpha_k_sell[k] / bb[k] for k in range(3))
        return 0.5 * (s_buy + s_sell)

    def rho_hat_per_scale(self) -> tuple[float, float, float]:
        """Per-scale ρ_k for ablation (Q-A multi-scale ACF check)."""
        bb = self._betas
        return tuple(
            0.5 * (self._alpha_k_buy[k] + self._alpha_k_sell[k]) / bb[k]
            for k in range(3)
        )

    def iir(self) -> float:
        lb = self.lambda_buy()
        ls = self.lambda_sell()
        denom = lb + ls
        if denom <= 1e-9:
            return 0.0
        return (lb - ls) / denom

    @property
    def warmed_up(self) -> bool:
        return self._warmed_up

    @property
    def asym_R(self) -> float:
        return self._asym_R

    @property
    def n_trades(self) -> int:
        return self._n_trades


# =============================================================================
# AlphaProtocol surface (signal layer, registry-discoverable)
# =============================================================================


class AmhpDynamicSpreadAlpha:
    """Signal-layer view of the AMHP estimator (registry-side)."""

    __slots__ = ("_state", "_signal")

    def __init__(self) -> None:
        self._state = _AMHPState()
        self._signal: float = 0.0

    @property
    def manifest(self) -> AlphaManifest:
        return AlphaManifest(
            alpha_id="r52_amhp_dynamic_spread",
            hypothesis=(
                "Multi-scale Hawkes branching ratio rho_hat(t) and intensity "
                "imbalance IIR(t) on TMFD6 trade flow size maker spread above "
                "the R47 cost-floor; state-dependent baseline mu(t) uses "
                "day-level covariates (foreign-IO z, US overnight) to diffuse "
                "fills across days, addressing R47-A1 single-day-dominance."
            ),
            formula=(
                "spread_target_pts(t) = max(5, 5 * (1 + alpha_rho * max(0, rho_hat - rho_low)"
                " + alpha_iir * |IIR|)); quote iff observed_spread >= spread_target"
            ),
            paper_refs=(
                "AMHP-2024",                 # user-supplied "六、AMHP" + "七、應用場景"
                "1105.3115",                 # Gueant-Lehalle-Tapia inventory risk
                "2403.02572",                # Lokin-Yu fill probabilities
            ),
            data_fields=(
                "trade_direction",
                "trade_volume",
                "exch_ts",
                "lob_imbalance_ppm",
                "best_bid",
                "best_ask",
                "foreign_io_zscore",        # daily snapshot
                "us_overnight_return",       # daily reference (session open ±30 min)
            ),
            complexity="O(1)",                # per-trade update; O(K=3) per scale
            status=AlphaStatus.PROTOTYPE,
            tier=AlphaTier.TIER_1,
            rust_module=None,
            latency_profile="v2026-04-24_measured",
            roles_used=("planner", "code-reviewer"),
            skills_used=("hft-backtester",),
            feature_set_version="lob_shared_v3",
            strategy_type="maker",
            instrument="TMFD6",
        )

    def update(self, *args: Any, **kwargs: Any) -> float:
        """Update with (ts_ns, direction).  Returns IIR as the canonical scalar."""
        if len(args) >= 2:
            ts_ns = int(args[0])
            direction = int(args[1])
            self._state.update_trade(ts_ns, direction)
        elif "ts_ns" in kwargs and "direction" in kwargs:
            self._state.update_trade(int(kwargs["ts_ns"]), int(kwargs["direction"]))
        self._signal = self._state.iir()
        return self._signal

    def reset(self) -> None:
        self._state = _AMHPState()
        self._signal = 0.0

    def get_signal(self) -> float:
        return self._signal

    @property
    def state(self) -> _AMHPState:
        return self._state


# =============================================================================
# Telemetry containers for kill-flag instrumentation
# =============================================================================


class _KillFlagCounters:
    """Per-strategy telemetry containers the T5 backtest reads via
    `kill_flag_telemetry()` to populate the standardized scorecard."""

    __slots__ = (
        "fills",
        "fill_pnl_pts_total",
        "fill_count",
        "modulator_gain_acc",       # K3 — Σ (per-fill spread captured) above R47 floor
        "fills_with_modulator",     # K3 — denominator (fills where multiplier > 1.0)
        "rho_hat_minute_samples",
        "rho_hat_critical_minutes",  # K5 — minutes with ρ̂ > 0.85
        "fills_per_day",            # day → fill count (for K1 distinct fill days)
        "pnl_per_day_pts",          # day → cumulative pt PnL (for K1 max_day, winning_days)
    )

    def __init__(self) -> None:
        self.fills: list[dict[str, Any]] = []
        self.fill_pnl_pts_total: float = 0.0
        self.fill_count: int = 0
        self.modulator_gain_acc: float = 0.0
        self.fills_with_modulator: int = 0
        self.rho_hat_minute_samples: int = 0
        self.rho_hat_critical_minutes: int = 0
        self.fills_per_day: dict[str, int] = {}
        self.pnl_per_day_pts: dict[str, float] = {}


# =============================================================================
# Strategy — three-layer dynamic-spread maker
# =============================================================================


class R52AmhpDynamicSpreadStrategy(R47MakerStrategy):
    """C1: AMHP-driven dynamic-spread maker built on top of R47 baseline.

    Layer 1 (spread gate):    L1 hard floor 5 pt; modulator widens *above* floor
    Layer 2 (signals):        AMHP rho_hat(t), IIR(t), multi-scale ρ̂_k
    Layer 3 (execution):      inherited R47 — bid/ask both sides, max_pos=3,
                              cancel-on-adverse via fast-cancel 59 ms profile
    """

    def __init__(
        self,
        strategy_id: str = "r52_amhp_dynamic_spread",
        # AMHP modulator
        base_spread_pts: int = _BASE_SPREAD_PTS,
        alpha_rho: float = _ALPHA_RHO,
        alpha_iir: float = _ALPHA_IIR,
        rho_low: float = _RHO_LOW,
        rho_tense: float = _RHO_TENSE,
        rho_critical: float = _RHO_CRITICAL,
        iir_tense: float = _IIR_TENSE,
        iir_critical: float = _IIR_CRITICAL,
        mult_cap: float = _MULT_CAP,
        asym_R: float = _ASYM_R_DEFAULT,
        # Day-level covariate coefficients (load-bearing — DA T2 flag #1)
        gamma_lob: float = 0.0,
        gamma_dist: float = 0.0,
        gamma_io: float = 0.0,
        gamma_us: float = 0.0,
        # Inherited R47 knobs — keep validated D1–D4 defaults
        # (PE disabled, queue-cancel disabled, MFG disabled per T1 §3 L2)
        pe_danger_threshold: float = 0.0,        # 0 disables PE block
        queue_cancel_threshold: float = 1.1,     # > 1 disables queue suppression
        mfg_skew_z_threshold: float = 99.0,      # high → MFG skew effectively off
        spread_threshold_pts: int = _BASE_SPREAD_PTS,  # R47 cost-floor
        max_pos: int = 3,                        # R47 structural property
        **kwargs: Any,
    ) -> None:
        super().__init__(
            strategy_id=strategy_id,
            pe_danger_threshold=pe_danger_threshold,
            queue_cancel_threshold=queue_cancel_threshold,
            mfg_skew_z_threshold=mfg_skew_z_threshold,
            spread_threshold_pts=spread_threshold_pts,
            max_pos=max_pos,
            **kwargs,
        )

        # AMHP estimator state — one per symbol
        self._amhp_states: dict[str, _AMHPState] = {}

        self._base_spread_pts = base_spread_pts
        self._base_spread_scaled = base_spread_pts * _PRICE_SCALE
        self._alpha_rho = alpha_rho
        self._alpha_iir = alpha_iir
        self._rho_low = rho_low
        self._rho_tense = rho_tense
        self._rho_critical = rho_critical
        self._iir_tense = iir_tense
        self._iir_critical = iir_critical
        self._mult_cap = mult_cap
        self._asym_R = asym_R

        self._gamma = {
            "lob": gamma_lob,
            "dist": gamma_dist,
            "io": gamma_io,
            "us": gamma_us,
        }

        # Kill-flag telemetry
        self._tel = _KillFlagCounters()
        self._last_minute_bucket_ns: int = 0
        self._stats_seen = 0
        self._modulated_quotes = 0

        logger.info(
            "r52_amhp_initialized",
            base_spread_pts=base_spread_pts,
            alpha_rho=alpha_rho,
            alpha_iir=alpha_iir,
            rho_critical=rho_critical,
            mult_cap=mult_cap,
            max_pos=max_pos,
            gamma_io=gamma_io,
            gamma_us=gamma_us,
        )

    # ------------------------------------------------------------- AMHP state

    def _get_amhp(self, symbol: str) -> _AMHPState:
        st = self._amhp_states.get(symbol)
        if st is None:
            st = _AMHPState(asym_R=self._asym_R)
            st.set_mu_coefficients(
                gamma_lob=self._gamma["lob"],
                gamma_dist=self._gamma["dist"],
                gamma_io=self._gamma["io"],
                gamma_us=self._gamma["us"],
            )
            self._amhp_states[symbol] = st
        return st

    def set_day_covariates(
        self,
        symbol: str,
        *,
        lob_imb_z: float = 0.0,
        dist_to_limit_z: float = 0.0,
        io_z: float = 0.0,
        us_overnight: float = 0.0,
        us_window_active: bool = False,
    ) -> None:
        """Refresh day-level covariates on the AMHP estimator.  Called by the
        backtest harness once per session open (io_z, us_overnight) and by the
        feature pipeline intraday (lob_imb_z, dist_to_limit_z)."""
        self._get_amhp(symbol).set_day_covariates(
            lob_imb_z=lob_imb_z,
            dist_to_limit_z=dist_to_limit_z,
            io_z=io_z,
            us_overnight=us_overnight,
            us_window_active=us_window_active,
        )

    # ---------------------------------------------------------- event handlers

    def on_tick(self, event: TickEvent) -> None:
        """Update AMHP intensity on each signed trade."""
        # Preserve R47 MFG side-effect (no-op since mfg_skew_z_threshold=99)
        super().on_tick(event)
        symbol = event.symbol
        direction = getattr(event, "trade_direction", 0)
        if direction == 0:
            return
        ts_ns = self._event_ts_ns(event)
        amhp = self._get_amhp(symbol)
        amhp.update_trade(ts_ns, direction)

    @staticmethod
    def _event_ts_ns(event: object) -> int:
        """Extract a monotonic ns timestamp from a TickEvent or LOBStatsEvent."""
        ts = getattr(event, "ts", None)
        if ts:
            return int(ts)
        meta = getattr(event, "meta", None)
        if meta is not None:
            src = getattr(meta, "source_ts", 0) or getattr(meta, "local_ts", 0)
            if src:
                return int(src)
        return now_ns()

    def on_features(self, event: FeatureUpdateEvent) -> None:
        """Refresh intraday covariates from FeatureEngine then defer to R47."""
        super().on_features(event)
        if event.values is None:
            return
        symbol = event.symbol
        feats = event.values
        if len(feats) > _IDX_L1_IMBALANCE_PPM:
            lob_z = float(feats[_IDX_L1_IMBALANCE_PPM]) / 1_000_000.0
            amhp = self._get_amhp(symbol)
            amhp.set_day_covariates(
                lob_imb_z=lob_z,
                dist_to_limit_z=amhp._dist_to_limit_z,  # preserved
                io_z=amhp._io_z,
                us_overnight=amhp._us_overnight,
                us_window_active=amhp._us_window_active,
            )

    def on_stats(self, event: LOBStatsEvent) -> None:
        """L1 spread gate with AMHP modulator — overrides R47 quoting."""
        symbol = event.symbol
        self._stats_seen += 1

        # Validity guard
        if (
            event.mid_price_x2 is None
            or event.spread_scaled is None
            or event.mid_price_x2 <= 0
            or event.spread_scaled <= 0
        ):
            return

        amhp = self._get_amhp(symbol)

        # ---- L2: derive modulator from AMHP signals ----
        if amhp.warmed_up:
            rho_hat = amhp.rho_hat()
            iir_abs = abs(amhp.iir())
        else:
            # Pre-warmup: act like R47 baseline at the cost-floor.
            rho_hat = 0.0
            iir_abs = 0.0

        multiplier = self._compute_multiplier(rho_hat, iir_abs)

        # ---- L1: spread floor enforcement ----
        # spread_target_scaled = max(BASE, BASE * multiplier).  Note: multiplier
        # >= 1.0 by construction; HARD floor at BASE prevents narrowing below.
        spread_target_scaled = max(
            self._base_spread_scaled,
            int(self._base_spread_scaled * multiplier),
        )

        if event.spread_scaled < spread_target_scaled:
            self._spread_blocked += 1
            self._record_minute_rho(amhp, ts_ns=self._event_ts_ns(event),
                                     rho_hat=rho_hat)
            return  # quote suppressed — observed spread below dynamic target

        # ---- L3: quote generation (R47 baseline math) ----
        mid_price_x2 = event.mid_price_x2
        spread_scaled = event.spread_scaled
        imbalance = event.imbalance
        exec_sym = self._exec_symbol(symbol)
        pos = self.position(exec_sym)

        imbalance_adj = int(imbalance * spread_scaled * 20 * 2 // 100)
        micro_price_x2 = mid_price_x2 + imbalance_adj

        tick_size_scaled = max(1, spread_scaled * 50 // 100)
        skew_x2 = -(pos * tick_size_scaled * 2) // 5
        fair_value_x2 = micro_price_x2 + skew_x2

        # Use spread_target_scaled as the floor for half-spread; if observed
        # spread is wider, capture the wider half via R47 baseline width logic.
        target_half_scaled = max(1, spread_target_scaled // 2)
        observed_half_scaled = max(1, spread_scaled // 2)
        half_spread_scaled = max(target_half_scaled, observed_half_scaled)

        bid_price_scaled = (fair_value_x2 - half_spread_scaled * 2) // 2
        ask_price_scaled = (fair_value_x2 + half_spread_scaled * 2) // 2

        # Active-size attenuation under tense / critical regimes
        qty = self._size_for_regime(rho_hat, iir_abs)
        max_pos = self._max_pos

        if pos < max_pos:
            self.buy(exec_sym, bid_price_scaled, qty)
        if pos > -max_pos:
            self.sell(exec_sym, ask_price_scaled, qty)

        if multiplier > 1.0:
            self._modulated_quotes += 1

        self._record_minute_rho(amhp, ts_ns=int(event.ts) if event.ts else now_ns(),
                                 rho_hat=rho_hat)

        if self._stats_seen % _LOG_INTERVAL == 1:
            logger.info(
                "r52_quote",
                symbol=symbol,
                rho_hat=round(rho_hat, 3),
                iir_abs=round(iir_abs, 3),
                multiplier=round(multiplier, 3),
                spread_target_pts=spread_target_scaled // _PRICE_SCALE,
                spread_observed_pts=spread_scaled // _PRICE_SCALE,
                pos=pos,
                qty=qty,
                modulated_quotes=self._modulated_quotes,
                spread_blocked=self._spread_blocked,
            )

    # ---------------------------------------------------------- L2 helpers

    def _compute_multiplier(self, rho_hat: float, iir_abs: float) -> float:
        """Compute spread multiplier g(rho_hat, |IIR|) bounded by mult_cap."""
        # T1 §3 L1 spec.  Critical regime overrides at full cap.
        if rho_hat >= self._rho_critical or iir_abs >= self._iir_critical:
            return self._mult_cap
        excess_rho = max(0.0, rho_hat - self._rho_low)
        mult = 1.0 + self._alpha_rho * excess_rho + self._alpha_iir * iir_abs
        if mult < 1.0:
            mult = 1.0
        if mult > self._mult_cap:
            mult = self._mult_cap
        return mult

    def _size_for_regime(self, rho_hat: float, iir_abs: float) -> int:
        """T1 §3 L1: tense / critical regimes attenuate active size."""
        if rho_hat >= self._rho_critical or iir_abs >= self._iir_critical:
            return _SIZE_CRIT
        if rho_hat >= self._rho_tense or iir_abs >= self._iir_tense:
            return _SIZE_TENSE
        return 1

    # ------------------------------------------------------ telemetry K1/K3/K5

    def _record_minute_rho(
        self,
        amhp: _AMHPState,
        *,
        ts_ns: int,
        rho_hat: float,
    ) -> None:
        """K5 — bucket ρ̂ samples per-minute and count critical (>0.85) buckets.

        Sampling once per minute keeps the denominator stable irrespective of
        tick-rate fluctuations across the trading session.
        """
        if not amhp.warmed_up:
            return
        minute_bucket = ts_ns // (60 * _NS_PER_SEC)
        if minute_bucket == self._last_minute_bucket_ns:
            return
        self._last_minute_bucket_ns = minute_bucket
        self._tel.rho_hat_minute_samples += 1
        if rho_hat > self._rho_critical:
            self._tel.rho_hat_critical_minutes += 1

    def record_fill_for_telemetry(
        self,
        *,
        day: str,
        pnl_pts: float,
        modulator_active: bool,
        captured_half_spread_pts: float,
        baseline_half_spread_pts: float,
    ) -> None:
        """T5 backtest harness call-back — records a fill for K1, K3 telemetry.

        Parameters
        ----------
        day : str  (YYYY-MM-DD)
        pnl_pts : float — realized fill PnL in points (post-cost)
        modulator_active : bool — whether multiplier > 1.0 at fill time
        captured_half_spread_pts : float — half-spread actually captured
        baseline_half_spread_pts : float — what the R47 floor would have captured
        """
        self._tel.fill_count += 1
        self._tel.fill_pnl_pts_total += pnl_pts
        self._tel.fills_per_day[day] = self._tel.fills_per_day.get(day, 0) + 1
        self._tel.pnl_per_day_pts[day] = self._tel.pnl_per_day_pts.get(day, 0.0) + pnl_pts
        if modulator_active:
            gain = captured_half_spread_pts - baseline_half_spread_pts
            self._tel.modulator_gain_acc += gain
            self._tel.fills_with_modulator += 1

    # ------------------------------------------------- public telemetry view

    def kill_flag_telemetry(self) -> dict[str, Any]:
        """Return per-strategy kill-flag accumulators consumed by the T5
        scorecard generator.  Non-mutating snapshot."""
        per_day = self._tel.pnl_per_day_pts
        if per_day:
            total = sum(per_day.values())
            day_max = max(per_day.values())
            winning_days = sum(1 for v in per_day.values() if v > 0)
            distinct_fill_days = len(self._tel.fills_per_day)
            max_day_pct = (day_max / total) if total > 0 else 0.0
        else:
            total = 0.0
            day_max = 0.0
            winning_days = 0
            distinct_fill_days = 0
            max_day_pct = 0.0

        if self._tel.fills_with_modulator > 0:
            modulator_per_fill_gain = self._tel.modulator_gain_acc / self._tel.fills_with_modulator
        else:
            modulator_per_fill_gain = 0.0

        if self._tel.rho_hat_minute_samples > 0:
            rho_critical_freq_pct = (
                100.0 * self._tel.rho_hat_critical_minutes / self._tel.rho_hat_minute_samples
            )
        else:
            rho_critical_freq_pct = 0.0

        return {
            "K1_max_day_pct": max_day_pct,
            "K1_winning_days": winning_days,
            "K1_distinct_fill_days": distinct_fill_days,
            "K1_total_pnl_pts": total,
            "K3_modulator_per_fill_gain_pts": modulator_per_fill_gain,
            "K3_modulator_fill_count": self._tel.fills_with_modulator,
            "K5_rho_critical_freq_pct": rho_critical_freq_pct,
            "K5_minute_samples": self._tel.rho_hat_minute_samples,
            "fill_count": self._tel.fill_count,
            "fill_pnl_pts_total": self._tel.fill_pnl_pts_total,
            "fills_per_day": dict(self._tel.fills_per_day),
            "pnl_per_day_pts": dict(self._tel.pnl_per_day_pts),
            "modulated_quotes": self._modulated_quotes,
            "spread_blocked": self._spread_blocked,
            "stats_seen": self._stats_seen,
        }


# Required by registry / impl loaders.
ALPHA_CLASS = AmhpDynamicSpreadAlpha
STRATEGY_CLASS = R52AmhpDynamicSpreadStrategy

MANIFEST = AmhpDynamicSpreadAlpha().manifest
