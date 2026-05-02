# R52 C1 — AMHP Dynamic-Spread Maker (TMFD6)

Round R52, Candidate C1. Run `alpha-research-20260425-hawkes-amhp`.

DA-approved at T2 (Tier 1/2 FAIL = 0; one bright-line cost-drag WARN; one S6 covariate-load-bearing WARN). Five conditional kill flags pre-registered for T6 — see `manifest.yaml` `kill_flags:` section.

## What this is

Adaptive Multi-scale Hawkes Process (AMHP) driven dynamic-spread maker, built on top of the validated R47 baseline three-layer pattern.

- **L1 spread gate** — `spread_target_pts(t) = max(5, 5 × g(ρ̂, |IIR|))`. Hard floor at 5 pt; never narrows below R47 cost-floor.
- **L2 signals** — three-scale exponential Hawkes (ms / 1 min / 1 hr decay rates) with state-dependent baseline `μ(t)` using day-level covariates `γ_io` (foreign-IO z) and `γ_us` (US overnight return ±30 min open). Asymmetric `α_sell_sell / α_buy_buy = 1.5` panic-sell prior.
- **L3 execution** — bid/ask both sides; `max_pos = 3` (R47 structural property); cancel-on-adverse via fast-cancel 59 ms profile.

## How it differs from killed directions

- vs **MLOFI-Hawkes (R27)** — non-directional both-sides quoted; modulator output is scalar magnitude in [1.0, 3.0], not a directional return prediction.
- vs **Omori-aftershock (R30)** — exponential not power-law kernel; own cost-edge math 4 / 8.25 = 48% (not 92%); maker not taker.
- vs **spread-conditional-maker (R16)** — external regime signal (ρ̂, IIR derived from trade arrivals upstream of spread) not observed-spread feedback loop; floor-not-target quoting.
- vs **R47-A1 single-day-dominance** — addressed by day-level covariates `γ_io`, `γ_us` in `μ(t)`. Diffusion is hypothesis-only at T2; falsifier is K1 in T5.

## Files

- `impl.py` — `AmhpDynamicSpreadAlpha` (signal-side, AlphaProtocol) + `R52AmhpDynamicSpreadStrategy` (R47MakerStrategy subclass).
- `manifest.yaml` — alpha metadata, parameters, latency profile, kill flags, A1-tightened criteria.

## Kill-flag instrumentation

The strategy exposes `kill_flag_telemetry()` returning per-K1/K3/K5 numeric accumulators. K2 (covariate CIs) and K4 (multi-scale ACF effect sizes) are computed offline by the T5 backtest harness from the trade tape and the rolling-MLE outputs; instrumentation hooks `set_day_covariates`, `set_mu_coefficients`, and `record_fill_for_telemetry` are provided.

## Float exception

Architecture Governance Rule §11 — `float` is permitted in this offline research alpha module. Live-path arithmetic (risk/order/execution) remains scaled int x10000. The `R52AmhpDynamicSpreadStrategy` keeps all price math in scaled int.

## Provenance

- T1 artifact: `docs/alpha-research/round-1-hawkes-amhp/artifacts/t1_researcher_c1.md`
- T2 artifact: `docs/alpha-research/round-1-hawkes-amhp/artifacts/t2_devils_advocate_c1.md`
- Profile: `v2026-04-24_measured` (submit/modify P95 = 395 ms, cancel P95 = 59 ms).
- Cost basis: `feedback_taifex_fee_structure.md` (TMF retail RT 4 pt = 40 NTD; confirmed 2026-03-26).
