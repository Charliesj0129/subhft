---
paths:
  - "**/*.py"
  - "**/*.pyi"
---

# Python Coding Style

> This file extends [common/coding-style.md](../common/coding-style.md) with Python specific content.

## Standards

- Follow **PEP 8** conventions
- Use **type annotations** on all function signatures

## Immutability

Prefer immutable data structures. **Note**: For HFT Hot Path, use `msgspec.Struct` or `NamedTuple` instead of `dataclass`.

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class User:
    name: str
    email: str

from typing import NamedTuple

class Point(NamedTuple):
    x: float
    y: float
```

## Formatting

- **ruff** for linting, code formatting, and import sorting (replaces black and isort)

## Reference

See skill: `python-patterns` for comprehensive Python idioms and patterns.
