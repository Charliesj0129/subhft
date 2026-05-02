# Module: trade_classifier

## Purpose

Classifies each tick as `buy`, `sell`, or `neutral` using bid/ask context.
Used by the normalizer to attach aggressor side to `TickEvent`.

## Contents

- `trade_classifier.py` (single file, lives at package root) — implements
  the Lee–Ready / tick-test hybrid used in the hot path.

## Used By

- `feed_adapter/normalizer.py` — invoked per tick during normalization.

## Notes

Must be allocation-free on the hot path. All state is held in pre-allocated
buffers. See `.agent/memory/module_gotchas.md` for edge-case handling.
