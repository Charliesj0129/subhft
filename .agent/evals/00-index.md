# Eval Harness Index

Evaluation definitions for hot-path components. Each eval contains:
- **Capability**: Functional requirements that must be satisfied.
- **Regression**: Performance and correctness regression checks.

## Components

| Eval | Component | Latency Target | File |
|------|-----------|---------------|------|
| Normalizer | `feed_adapter/normalizer.py` | < 50us (Python) / < 5us (Rust) | [normalizer.md](normalizer.md) |
| LOB Engine | `feed_adapter/lob_engine.py` | < 100us per update | [lob-engine.md](lob-engine.md) |
| Risk Guard | `risk/` | < 10us per validation | [risk-guard.md](risk-guard.md) |

## How to Use

1. When modifying a hot-path component, review its eval definition first.
2. Ensure all **Capability** checks pass in unit tests.
3. Run benchmarks to verify **Regression** targets are met.
4. Update the eval if new capabilities are added.
