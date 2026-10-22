# Eval Harness Index

## Skill trigger cases (ACTIVE)

[skill-triggers.md](skill-triggers.md) — task statements mapped to the skill
that should load, plus counter-examples. Run after any skill description
change (procedure in that file).

## Component evals (legacy)

The component eval definitions below are the 2026-02/03 generation —
historical reference, not maintained (see `.agent/00-MANIFEST.md`).

Evaluation definitions for hot-path components. Each eval contains:

- **Capability**: Functional requirements that must be satisfied.
- **Regression**: Performance and correctness regression checks.

## Components

| Eval | Component | File |
| --- | --- | --- |
| Normalizer | `feed_adapter/normalizer.py` | [normalizer.md](normalizer.md) |
| LOB Engine | `feed_adapter/lob_engine.py` | [lob-engine.md](lob-engine.md) |
| Risk Guard | `risk/` | [risk-guard.md](risk-guard.md) |

Other hot-path components have no eval file; use their unit tests and
`tests/benchmark/`.

## How to Use

1. When modifying a covered component, review its eval definition first.
2. Ensure all **Capability** checks pass in unit tests.
3. Run benchmarks to verify **Regression** targets are met.
4. Update the eval if new capabilities are added.

## Running Benchmarks

```bash
make benchmark                 # all; make benchmark-compare against baseline

# Specific component
uv run pytest tests/benchmark/micro_bench_normalizer.py -v
uv run pytest tests/benchmark/micro_bench_lob.py -v
```
