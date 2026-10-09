---
name: symbols-sync
description: "Regenerate and validate the symbol universe (symbols.list to symbols.yaml), including the post-contract-roll rebuild and HFT_SYMBOLS. Use after a contract roll, when listings change, or when subscriptions skip expired codes. Not for broker session debugging."
---

# Symbols sync

`config/symbols.yaml` is operator-regenerated and Do-NOT-Edit by hand;
pool-mode engines never rebuild it at runtime (an in-process rewrite once
corrupted the per-connection partitions). Rebuild it offline after each roll.

```
config/symbols.list --(hft config build / sync)--> config/symbols.yaml --> runtime filter HFT_SYMBOLS
```

## Procedure

```bash
make rebuild-symbols-yaml          # regenerate from the current contracts cache (after a roll)
uv run hft config preview          # expanded universe
uv run hft config validate
git diff config/symbols.yaml       # month codes (E6->F6->G6) and TXO strikes should evolve; stocks stay stable
```

Load the right broker credentials first (the generator uses broker contract
metadata; never print them). `uv run hft config sync --loop <id>` builds a loop's
universe (default subscription cap 8 with `--loop`). Commit the regenerated file
and restart the engine only with the user's approval (production restarts follow
`41-deployment.md`). Full runbook: `docs/runbooks/SymbolsYamlRegeneration.md`.

Contract rolls happen around the third Wednesday (TXF/MXF/TMF/EXF). A pool-mode
universe that does not roll is a known open risk; check
`.agent/memory/current-risks.md`.

## Verify after regeneration

Each symbol exists on the selected broker; `scale` stays 10000; exchange, lot
size, and tick size look right; the runtime can subscribe to the universe.
Stale hand-edited metadata means regenerate, not patch.

## HFT_SYMBOLS

`export HFT_SYMBOLS="2330,TX00"` filters the generated universe for narrow runs.
It is a runtime filter, not a substitute for syncing contract metadata. If a
subset override is ignored, export it in the current environment.

## Done when

`validate` passes, the diff is reviewed, and `symbols.yaml` was not edited by hand.
