# ADR-[NUMBER]: [TITLE]

## Status
[Proposed | Accepted | Deprecated]

## Context
What is the problem? Why is it hard? What are the constraints?
(e.g., "We need to store Order Book snapshots, but writing JSON to disk is too slow and blocks the tick loop.")

## Decision
What are we going to do?
(e.g., "We will use SBE (Simple Binary Encoding) over a shared memory ring buffer.")

## Consequences
### Positive
- Latency reduced by X us.
- Zero-copy achieved.

### Negative
- Complexity increased.
- Python clients need a C extension to read data.

## Compliance
- [ ] Allocator Law checked?
- [ ] Async Law checked?
