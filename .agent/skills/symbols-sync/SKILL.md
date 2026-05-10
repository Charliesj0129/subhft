---
name: symbols-sync
description: Use when updating the symbol universe, regenerating config/symbols.yaml from broker contracts, validating symbol metadata, or diagnosing HFT_SYMBOLS override behavior.
---

# Symbol Universe Sync

Use this skill for the generated symbol pipeline, not for broker-session debugging.

## Canonical Flow

Treat the symbol flow like this:

```text
config/symbols.list -> make sync-symbols -> config/symbols.yaml -> runtime filtering via HFT_SYMBOLS
```

Edit `config/symbols.list`, regenerate `config/symbols.yaml`, then validate the resolved config.

## Workflow

```bash
make sync-symbols
uv run hft config preview
uv run hft config validate
```

Load the correct broker credentials before syncing, because the generator depends on broker contract metadata.

## What To Verify

Verify these after regeneration:

- each symbol exists on the selected broker
- `scale` remains `10000`
- exchange, lot size, and tick size look correct
- the runtime can subscribe to the generated universe

## HFT_SYMBOLS Override

Use `HFT_SYMBOLS` to filter the generated universe for narrow test runs:

```bash
export HFT_SYMBOLS="2330,TX00"
```

Treat `HFT_SYMBOLS` as a runtime filter, not a replacement for syncing the underlying contract metadata.

## Failure Patterns

| Symptom | Action |
| --- | --- |
| symbol lookup fails during sync | inspect symbol format and broker availability |
| generated metadata looks hand-edited or stale | regenerate instead of patching `config/symbols.yaml` manually |
| runtime ignores the subset override | export `HFT_SYMBOLS` in the current shell or environment |
