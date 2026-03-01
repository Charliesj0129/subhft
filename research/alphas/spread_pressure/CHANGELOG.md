# Changelog: spread_pressure

## 2026-02-28 — Initial implementation (Gate A–B)

- Scaffolded alpha package structure.
- Implemented `SpreadPressureAlpha` with `__slots__`, O(1) computation, no heap
  allocation per tick (Allocator Law).
- All 3 feature inputs are scaled integers ×10000; division gives float for
  ranking only — never used as price (Precision Law).
- `latency_profile` set to `shioaji_sim_p95_v2026-02-28` at inception
  (constitution requirement; enables Gate D path without retroactive fix).
- 18 Gate B tests passing.
- Status: `DRAFT` (advances to `GATE_B` after CI green + review).
