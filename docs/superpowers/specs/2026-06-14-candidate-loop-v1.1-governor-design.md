# Candidate Loop v1.1 — Governor Design

Date: 2026-06-14
Status: design approved (awaiting spec review)
Scope owner: research / candidate loop
Related: `docs/research/alpha_candidate_loop_v1_spec.md` (frozen v1.0 contract),
`.claude/plans/repo-brief-majestic-quasar.md` (v1.0 landing plan),
`docs/goals/candidate_research_refinement_loop.md` (sibling refinement loop)

## 1. Objective

The v1.0 candidate loop runs a *fixed* set of generation prompts (`prompts/v1/<family>.md`),
produces a batch of candidates, scores them through frozen gates, and emits
`runs/<run_id>/failure_summary.json`. Nothing reads that summary back: the next
round's prompts are identical to the last round's. The loop has no feedback.

The **v1.1 governor** closes the loop. It reads `failure_summary.json`, derives a
**deterministic, human-readable steering brief per family** (where to push, where
to back off, this round), the human reviews/edits/approves the brief, and then a
cheap LLM (DeepSeek, OpenAI-compatible) generates the next round's candidate JSONL
*within that approved steering* — using the **unchanged frozen base prompt** plus
the approved brief as added context. The existing `generate` → `run` pipeline
consumes the drop exactly as it consumes the template expander's output today.

This targets the assumption-quality bottleneck identified across 14+ killed
candidates: generation cost is already negligible, so the leverage is *steering*
generation toward the families/parameter regions the evidence says are alive
(this round: `trade_flow`, median survivor IC ≈ 0.114, 2 maker-rescuable
near-misses) and away from the dead ones (`microprice`, 100% cost-killed) — not
brute-forcing more undirected candidates.

## 2. Non-negotiable principle: the governor lives *upstream* of the frozen loop

The governor only **reads** `failure_summary.json` and **writes** candidate JSONL
plus provenance sidecars. It changes nothing in the scored path:

- No change to `evaluator.py` / `scoring.py` / gates / split definitions.
- No change to the ClickHouse schema (`research.alpha_candidates`,
  `research.experiment_results`).
- No change to any frozen version string (`primitive_version`,
  `evaluator_version`, `scoring_version`, `cost_assumption_version`, …).
- No `src/` import of the governor; live registry stays FROZEN (`r47_tmf_v1`).
- The >10 pt/trade quality floor and all gate thresholds are untouched.

If the governor produced nothing, the loop would still run exactly as v1.0. That
containment is what makes a non-deterministic LLM safe to bolt on.

## 3. The frozen-prompt tension and why steering is a separate layer

`prompts/v1/<family>.md` are **functional inputs, not docs**: `generate.build_header`
reads their frontmatter for `prompt_id` and hashes their exact bytes into
provenance (`prompt_sha256`), and `tools/render_prompts.py` regenerates them from
`schema.py` so they stay mechanically in sync with the `prim_v1` signatures and
window/horizon domains. `tests/.../test_prompts.py` asserts the committed files
match a fresh render **byte-for-byte**.

Therefore the governor **cannot hand-edit the base prompts** — doing so would
break `test_prompts.py` and silently fork the grammar contract. The frozen base
prompt defines the *contract* (grammar, schema, primitives, output shape); the
variable, steerable part must be a **separate layer**. That layer is the steering
brief, passed to the LLM as additional context alongside — never instead of — the
frozen base prompt.

## 4. Architecture: deterministic brief, LLM generation, human gate between

The single architectural decision (approved): **the steering brief is
deterministic** (rule-based derivation from `failure_summary`), not LLM-written.
The LLM's intelligence is spent only where it is worth paying for — generating
concrete candidate formulas *within* the steered focus — while the brief stays
transparent, reproducible, and auditable so the human review is meaningful. An
LLM-drafted brief is a future toggle, explicitly out of scope for v1.1.

The **human approval gate sits on the brief**: each brief carries
`approved: false` in its frontmatter, and `governor generate` hard-refuses any
family still at `false`. Approval is explicit, per-family, and auditable (the
approved brief's sha256 is recorded in provenance).

### Data flow

```
runs/<prior_run>/failure_summary.json
   │  governor draft   (deterministic; signals.py + brief.py)
   ▼
runs/<prior_run>/steering/<family>.md   ×6   (frontmatter: approved: false)
   │  ←── 【HUMAN edits prose / focus / n_target, flips approved: true】  ── GATE
   ▼
governor generate   (refuses any family with approved: false)
   │  client.py → DeepSeek: frozen base prompt + approved brief → raw JSONL
   │  raw drop frozen at: candidates/<gen_run>/_governor_raw/<family>.jsonl
   ▼
generate_drop(prompt=prompts/v1/<family>.md, from_jsonl=raw drop,
              generation_model="deepseek-<model>")        ← UNCHANGED v1.0 fn
   │
   ▼
candidates/<gen_run>/family=<family>.jsonl   (+ governor_manifest.json sidecar)
   ▼
run --batch <gen_run>     ← UNCHANGED frozen loop
```

## 5. Module map — new isolated sub-package `research/candidate_loop/governor/`

Kept in its own sub-package so the frozen loop never imports it.

| Module | Responsibility |
| --- | --- |
| `signals.py` | Parse `failure_summary.json` → a typed per-family `SteeringSignals` (survival_rate, ic p10/p50/p90, cost/maker cost failure rates, maker_rescuable_count, near_misses, duplicate_rate, reduced_day_coverage_count) + a derived `focus` label (`amplify` / `maintain` / `deprioritize` / `retire`) from config thresholds. **Pure, no IO beyond reading the JSON.** |
| `brief.py` | Render `steering/<family>.md`: YAML frontmatter (`family`, `approved: false`, `focus`, `n_target`, the signals snapshot, `source_run_id`) + a human-readable **Why / Focus / Avoid** body derived from the signals. Also parse a brief back and expose `is_approved`. |
| `client.py` | Thin DeepSeek httpx client. OpenAI-compatible `POST {base_url}/chat/completions`; key from `DEEPSEEK_API_KEY` env only; bounded (`max_tokens`, `timeout`, `max_retries`, `temperature`); **redacts the key, never logs it**; TLS verify on. Returns parsed candidate JSONL lines (shape-checked, content validation deferred to the runner). |
| `runner.py` | `draft` and `generate` orchestration. `draft`: failure_summary → signals → briefs on disk. `generate`: enforce the approval gate per family, call the client, freeze the raw drop, hand it to `generate_drop`, and write `governor_manifest.json` provenance sidecar. |
| `config` | `config/research/candidate_loop/governor_v1.yaml` — focus thresholds, `n_target` defaults per focus, model name, client bounds. Config-driven like `scoring_v1.yaml` / `evaluator_v1.yaml`. |
| CLI (extend `__main__.py`) | `governor draft --from-run <prior> [--out runs/<prior>/steering]` ; `governor generate --steering <dir> --gen-run <id> [--model deepseek-chat] [--n-per-family N]`. |

## 6. The deterministic steering brief

### 6.1 Signals read (exact `failure_summary.per_family[<family>]` fields)

All of these already exist in `failure_summary.build_failure_summary` — the
governor adds **no** new summary fields:

- `survival_rate` — fraction passing all gates on train.
- `ic_distribution_survivors` — `{p10, p50, p90}` of validation IC for survivors.
- `cost_failure_rate`, `maker_cost_failure_rate` — taker / maker cost-gate kill rate.
- `maker_rescuable_count` — failed taker cost gate, passed maker (the pool a
  maker-execution variant could revive). This is the steering signal that
  distinguishes "dead" from "dead as a taker, alive as a maker".
- `near_misses` — up to 5 single-gate failures with signed `margin` to the
  threshold (sorted closest-first). Tells the brief *which* knob to nudge.
- `duplicate_rate` — fraction killed `DUPLICATE_ALPHA`; high → the family's
  parameter space is being re-sampled, push for novelty.
- `reduced_day_coverage_count` — candidates whose `effective_day_count <
  day_count` (the `trade_flow` dir-clean mask cost); reported so the human knows
  a family's evidence is thinner than nominal.
- `common_failure_patterns`, `invalid_formula_rate` — grammar/schema friction to
  flag back into the brief prose.

### 6.2 Focus labels (config-driven, deterministic)

`governor_v1.yaml` declares the thresholds; `signals.py` applies them in a fixed
priority order so the label is reproducible. Proposed default rule (subject to
review):

- **`retire`** — `survival_rate == 0` AND `maker_rescuable_count == 0` AND
  `ic_p50` below the `no_signal` floor. Family is dead with no maker rescue path:
  drop to a minimal probe count next round.
- **`deprioritize`** — `survival_rate` below `low` threshold AND few/no
  near-misses with small margin. Keep a small probe allocation.
- **`maintain`** — middling survival; keep `n_target` flat.
- **`amplify`** — `survival_rate` above `high` threshold OR `ic_p50` strong OR
  `maker_rescuable_count > 0` OR near-misses with small margin (a cheap nudge
  could flip them). Raise `n_target`.

The labels only change `n_target` (how many candidates to request) and the brief
prose. They never touch gates or scoring. `n_target` per focus comes from config.

### 6.3 Brief file format

```markdown
---
family: trade_flow
source_run_id: smoke_001
approved: false          # ← human flips to true to authorize generation
focus: amplify
n_target: 30
signals:
  survival_rate: 0.10
  ic_p50: 0.114
  cost_failure_rate: 0.55
  maker_cost_failure_rate: 0.40
  maker_rescuable_count: 2
  duplicate_rate: 0.05
  reduced_day_coverage_count: 7
generated_at: "2026-06-14T..."   # the only non-deterministic field; CLI stamps it
---

# Steering brief — trade_flow (focus: amplify)

## Why
Survivor IC median 0.114 with 2 maker-rescuable near-misses — the live-est
family this round.

## Focus
- Push signed-flow formulas that the maker cost view rescued (see
  maker_rescuable_count).
- The two near-misses failed `cost_proxy_taker` by a small margin — vary
  smoothing window / horizon around them.

## Avoid
- Re-sampling formulas already tried (duplicate_rate climbing).
- Anything that needs days masked out by the dir-clean filter
  (reduced_day_coverage_count = 7).
```

The human may freely edit Focus/Avoid prose, adjust `n_target`, change `focus`,
then set `approved: true`. The edited file's bytes are what gets hashed into
provenance — so the approved brief is exactly what steered generation.

## 7. DeepSeek client and generation

- OpenAI-compatible: `base_url=https://api.deepseek.com`, `POST /chat/completions`,
  `model` from config (default `deepseek-chat`; the exact "Pro" SKU is confirmed
  at integration time — config string, no code change to switch).
- Raw `httpx` (already a dependency); no new SDK pin.
- Request = system/user messages = **frozen base prompt file contents** + the
  **approved steering brief body** + the explicit "emit exactly N JSONL lines"
  instruction already in the base prompt. The base prompt's output contract
  (one JSON object per line, no fences, validate against `candidate.schema.json`)
  is unchanged.
- Bounded: `max_tokens`, `timeout`, `max_retries`, low `temperature` from config.
- The client shape-checks each returned line is a JSON object (same contract as
  `generate.ingest_jsonl`); it does **not** content-validate — invalid candidates
  must reach the runner to be recorded as INVALID with a death reason (spec §13).

## 8. Provenance and idempotency

- **Generation model**: `generate_drop(..., generation_model="deepseek-<model>")`
  flows through the **existing** `_header` provenance line — it was `template_v1`
  before, now a different string value. No schema change.
- **Frozen prompt hash**: `generate.build_header` still hashes the frozen base
  prompt file (`prompt_sha256`), proving which contract version generated the drop.
- **Steering provenance sidecar**: `governor generate` writes
  `candidates/<gen_run>/governor_manifest.json` with, per family:
  `source_run_id`, `steering_path`, `steering_sha256` (of the approved brief
  bytes), `focus`, `n_target`, `model`, `generated_at`. This keeps the frozen
  `_header` contract untouched while making the full steering chain auditable.
- **Raw drop frozen**: the LLM response is non-deterministic, so the raw drop is
  saved at `candidates/<gen_run>/_governor_raw/<family>.jsonl` **before** it is
  fed to `generate_drop`. Re-running `governor generate` for the same `gen_run`
  reuses the saved raw drop (it does **not** re-call DeepSeek) — that is the
  idempotency contract for the LLM step. The deterministic `draft` step
  re-produces byte-identical briefs.
- **Downstream idempotency unchanged**: `run --batch` already dedupes by
  `alpha_id` and caches panels, so re-running the batch yields zero new rows.

## 9. Security (hard requirements)

- `DEEPSEEK_API_KEY` lives in `.env` only; never logged, committed, printed, or
  passed as a CLI arg. The client reads it from the environment and **redacts**
  it from any error/log surface.
- TLS verification on for all DeepSeek calls.
- Raw LLM responses are alpha formulas (no secrets), but the client still scrubs
  before any log line.
- Fail-closed if the key is missing: `governor generate` errors with a clear
  message and exits non-zero; it never silently degrades to an unauthenticated
  call.

## 10. Testing (≥80% coverage, behavior-named, in `tests/unit/research/candidate_loop/governor/`)

- **signals**: a fixture `failure_summary.json` → known per-family signals and
  known focus labels (one fixture per focus class; boundary cases at each
  threshold). `retire`/`amplify` priority order is pinned.
- **brief**: renders `approved: false` by default; round-trips
  (render → parse → `is_approved` False); editing `approved: true` parses True;
  byte-stable re-render for the same signals **with an injected fixed timestamp**
  (the only non-deterministic frontmatter field; the renderer takes
  `generated_at` as a parameter so the deterministic body is independently
  assertable).
- **gate**: `governor generate` **refuses** any family whose brief is
  unapproved (fail-closed), and proceeds only for approved families.
- **client** (mocked transport): parses valid JSONL; raises on non-object lines;
  fails closed when `DEEPSEEK_API_KEY` is absent; **never emits the key** in any
  message (assert redaction); respects `max_tokens`/`timeout` config.
- **provenance**: end-to-end with a **mocked** DeepSeek client → raw drop frozen
  → `generate_drop` stamps `generation_model="deepseek-…"` + frozen
  `prompt_sha256`; `governor_manifest.json` records the approved brief's sha256.
- **idempotency**: second `governor generate` for the same `gen_run` reuses the
  frozen raw drop and does not call the client.

## 11. Boundaries / out of scope (tracked gaps, not silent drops)

- **No FDR / OOS confirmation layer.** Steered generation produces fewer, targeted
  candidates per round (gentler on multiple-testing than brute force), but the
  governor does **not** add false-discovery control or an OOS-confirmation gate.
  This remains the known v1.2 gap — top-1% promotion over many candidates still
  needs an FDR/holdout layer before any promotion is trustworthy.
- **No LLM-drafted briefs.** The brief is deterministic in v1.1; LLM prose is a
  future toggle.
- **No auto-approval.** The human gate is mandatory; there is no flag to skip it.
- **No frozen-loop changes.** Gates, scoring, schema, versions, the >10 pt floor,
  and the live registry are all untouched.
- **No mass exploration.** v1.2 scale-up (e.g. 600+ candidates) is out of scope;
  v1.1 is the feedback mechanism, deliberately small per round.

## 12. Open questions for review

1. **Focus thresholds** (§6.2) — are the proposed default cutoffs and the
   `n_target`-per-focus mapping right, or should they start more conservative
   (e.g. `amplify` never more than 1.5× the family's prior count)?
2. **DeepSeek model string** — confirm the exact model id to default in
   `governor_v1.yaml` (`deepseek-chat` vs the "Pro" SKU).
3. **Raw-drop retention** — keep `_governor_raw/` in the run tree indefinitely
   for audit, or gitignore it (it is LLM output, re-derivable only by re-calling
   a non-deterministic API, so retaining it is the auditable choice; it should be
   gitignored from commits but kept on disk).
