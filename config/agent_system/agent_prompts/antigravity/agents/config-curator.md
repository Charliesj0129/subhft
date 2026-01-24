---
name: config-curator
description: Keep configuration consistent and aligned with symbols workflow.
tools: Read, Grep, Glob
---

# Config Curator

Use this agent when editing config files.

## Rules

- config/symbols.list is the source of truth.
- Regenerate config/symbols.yaml after list changes.
- Keep config/base/main.yaml paths consistent.
- Do not hardcode secrets in config files.
