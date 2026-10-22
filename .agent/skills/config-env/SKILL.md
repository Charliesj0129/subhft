---
name: config-env
description: "How config resolves (YAML layers, loop binding, HFT_* env vars, CLI overrides), where settings live, and credential rules. Use when changing settings, debugging why a flag has no effect, or adding an env toggle. Not for broker adapter code or secret values."
---

# Config and environment

Never read, print, or commit secret values. `.env*` and `config/settings.py`
are denied to tools; set variable names and shapes only.

## Resolution

Later layers override earlier ones: `config/base/main.yaml` -> `config/env/<mode>/main.yaml`
-> per-machine `config/settings.py` (gitignored) -> `HFT_*` environment variables
-> CLI flags. Loader: `src/hft_platform/config/loader.py`.

Loop binding (loop_v1): `uv run hft run --loop <id>` reads `config/loops/<id>.yaml`,
which overrides `strategy` and `broker`, and requires the strategy to be
`enabled: true` in the registry (`LoopBindingError` otherwise). Live registry is
frozen to `r47_tmf_v1`.

Other files: `config/base/brokers/<broker>.yaml`, `config/risk.yaml`,
`config/base/session_governor.yaml`, `config/research/latency_profiles.yaml`,
`config/symbols.yaml` (see `symbols-sync`).

## Variables

The complete, guarded reference is `docs/operations/env-vars-reference.md`;
`make env-vars-guard` checks it against the code. Read the code for a variable's
real default before relying on any doc. The ones that change behavior most:

| Variable | Note |
|---|---|
| `HFT_MODE` | `sim` / `live` / `replay` |
| `HFT_ORDER_MODE` | `sim` still dispatches; only `disabled` stops orders; `live` is real money and a red line |
| `HFT_BROKER` | `shioaji` (default) or `fubon` (see `broker-integration`) |
| `HFT_RECORDER_MODE` | `direct` or `wal_first` (see `hft-recorder`) |
| `HFT_GATEWAY_ENABLED`, `HFT_STRICT_PRICE_MODE` | gateway dispatch; reject float prices |
| `HFT_STORMGUARD_FEED_GAP_STORM_S` | feed gap to STORM; a gap alone cannot HALT (`..._HALT_S` is a deprecated alias for STORM) |
| `HFT_RECONNECT_*`, `HFT_QUOTE_FLAP_*` | reconnect window and flap detection |

Adding a toggle: read it once at startup (not per tick), give it a safe default,
document it in `env-vars-reference.md`, run `make env-vars-guard`. A config value
copied into a prompt or doc goes stale; grep `.agent/` and docs when a number changes.

## Secrets

Prefix-isolated (`SHIOAJI_*`, `HFT_FUBON_*`, `HFT_*`, `CLICKHOUSE_*`, `HFT_TELEGRAM_*`);
`.env` only; never in code, logs, CLI args (visible in `ps`), or commits. Verify
with `git check-ignore .env`. Rotate anything that may have leaked.

## Done when

The value resolves as intended in `uv run hft config validate` (or the relevant
loader test), `make env-vars-guard` passes if variables changed, and no secret
appeared in output or diff.
