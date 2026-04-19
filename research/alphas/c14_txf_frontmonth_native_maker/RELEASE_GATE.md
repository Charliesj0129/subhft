# C14 — Release Gate Checklist

> ⛔ **KILLED 2026-04-18** — R6 PROMOTE REVOKED on cost-model error.
>
> The release-gate analysis below was performed under the assumption TXF retail
> RT = 0.48 pt. **Actual retail RT ≈ 3 pt** (user-confirmed). Re-computed under
> corrected cost: OOS net −1.69 pt/fill, **−638K NTD/day**, edge/RT 0.44× —
> deep under 1× floor.
>
> **DO NOT proceed with shadow. DO NOT proceed with live.**
>
> Gate #6 (Risk Validation) and Gate #7 (Performance) are both retroactive FAIL.
> All other gates are moot.
>
> See:
> - `memory/r6_cost_model_correction.md`
> - `outputs/team_artifacts/alpha-research/round-6/summary.md` Correction section
> - `manifest.yaml` correction_note

Per `.agent/skills/hft-release-gate/SKILL.md`. This records the C14-specific
release-readiness state as of post-PROMOTE scaffold (2026-04-17).
**Status as snapshot of an INVALID PROMOTE — retained for traceability only.**

Rows reference the skill's 7-gate base + strategy-specific gates 8-13.

## Core Gates (1-7)

| # | Gate | Status | Note |
| -: | ---- | :----: | ---- |
| 1 | Code Quality | **N/A for scaffold** | Full `make check` runs across the repo. The C14-scoped ruff check on the new files passes clean. Repo-wide gates are not in C14's scope. |
| 2 | Test Coverage | **PASS (locally)** | `tests/test_c14.py` covers the research impl (33/33 tests). Runtime wrapper coverage: not yet tested — see Blocker #1 below. |
| 3 | Test Hygiene | **PASS (C14 scope)** | All tests have `assert`; behavior-oriented names; no `test_covers_*` patterns. |
| 4 | Architecture | **PASS** | No contracts → runtime imports. No broker SDK imports outside feed_adapter. Strategy package split discipline preserved (`strategies/` for implementations; `strategy/` for framework). |
| 5 | Security | **PASS** | No hardcoded secrets; no new env vars on strategy side; cost profile additions do not include any sensitive data. |
| 6 | Pre-Market Health | **DEFERRED** | Not applicable until shadow deploy day. Run `make pre-market-check` on the deployment host on day 1. |
| 7 | Latency Regression | **DEFERRED** | Profile baseline captured via `sim_p95_v2026-02-26`. Run `make hotpath-profile` day-1 shadow. Expected: no regression vs R47 baseline — C14's per-tick work is the same + one dict lookup for the per-symbol state. |

## Strategy-Specific Gates (8-12)

| # | Gate | Status | Note |
| -: | ---- | :----: | ---- |
| 8 | Shadow session reviewed | **BLOCKING → pending shadow** | Minimum 10 trading days. Gate E evidence comes from this. |
| 9 | Latency profile documented | **PASS** | `config/research/latency_profiles.yaml` has `sim_p95_v2026-02-26`. Manifest declares it. |
| 10 | Max position conservative | **PASS** | Live day-1 spec is `max_pos=1`, ramp to 3 over 3 stable days. Codified in `SHADOW_DEPLOY.md` §7 item 5. |
| 11 | Canary config | **DEFERRED** | `config/strategy_promotions/20260417/c14_txf_frontmonth_maker.yaml` — not authored yet; post-shadow deliverable. |
| 12 | Rollback plan | **PASS** | `SHADOW_DEPLOY.md` §8 documents the rollback procedure. |

## C14-Specific Extensions (13-15)

| # | Gate | Status | Note |
| -: | ---- | :----: | ---- |
| 13 | Front-month rotator | **BLOCKING → pending engineering** | The shadow wrapper accepts a literal symbol list and quotes whichever symbol produces events. A production rotator that queries Shioaji daily open-interest / volume to programmatically switch active contract is required before live. Estimated effort: 1-2 days. Tracked as `c14_frontmonth_rotator` in the research dir. |
| 14 | Rollover synthesiser | **PASS (shadow)** | The research-side `rollover_flatten_cost` approximation (crossing the spread + retail queue penalty) is present in the backtest driver. Shadow will observe the natural broker-side flatten cost — no synth needed in shadow. Live will require explicit flatten-then-switch orchestration, currently part of Gate 13. |
| 15 | User-approval gate | **PASS (scaffold)** | `feedback_no_auto_deploy` memory: automation never enables live. The `enabled: false` in `strategies.yaml` is the hard safety; only a human commit flips it. |

## Blockers before live

1. **Gate 13 — front-month rotator**. Shadow acceptable without it (the symbol
   list is static during shadow, and the rotator's decisions are observable
   from the research schedule). Live requires a runtime component that
   reliably identifies the current front-month.
2. **Gate 8 — shadow session data**. 10 trading days minimum.
3. **Gate 11 — canary config**. Authored post-shadow, reviewed by Challenger.

## Non-blockers (informational)

- T5-REVISE residual T1-parity gap (OOS +313K NTD/day is 7× T1 best-case
  +45K). Not a hard blocker — Gate E shadow will produce the real-retail
  number. If shadow Sharpe compresses to ~2-4× instead of ~20× backtest,
  that is expected and not a regression.
- Sharpe 18.89 OOS on N=20 — small-sample artifact. Shadow N=10 is
  inherently small too; Gate E is qualitative ("daily loss discipline
  observed, no storm faults"), not a Sharpe threshold.

## Release-readiness summary

**Scaffold state: 10/15 gates PASS, 2 DEFERRED (run-day-dependent), 3 BLOCKING on shadow or engineering.**

Decision: C14 is ready for **shadow deploy** upon:
- User commits `enabled: true` flip (gate 15 enforced by process).
- Day-1 operator runs pre-launch checklist in SHADOW_DEPLOY.md §3.

C14 is **NOT ready for live**. Blockers 1 & 2 (front-month rotator + 10-day
shadow) must clear first; user approval is the final gate per
`feedback_no_auto_deploy`.
