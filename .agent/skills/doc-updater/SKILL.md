---
name: doc-updater
description: Auto-generate codemaps and reconcile architectural docs with the current source tree. Use when docs drift from code, when regenerating `docs/CODEMAPS/structure.md`, or when verifying `docs/ARCHITECTURE.md` still matches `src/`.
---

# Doc Updater

Cartographer persona — keeps docs in sync with code.

## Capabilities

1. **Generate Codemap**
   - Scan `src/` and `config/`.
   - List each module with its primary responsibility (module docstring).
   - Write to `docs/CODEMAPS/structure.md`.

2. **Reconcile Architecture**
   - Read `docs/ARCHITECTURE.md`.
   - If code diverges from the doc: either update the doc (when code is right) or flag a governance violation (when the doc is authoritative).

## Usage

`"Update the codemaps"` → runs the scan and rewrites the markdown.
