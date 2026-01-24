# Naming Conventions

Keep names consistent across the repo.

## Files
- `snake_case.py` for modules.
- `kebab-case.md` for docs.
- `NNN_title.md` for ADRs.

## Python
- Classes: `PascalCase`
- Functions: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Private members: `_leading_underscore`

## Config
- YAML keys: `snake_case`
- Enum-like values: `lower_snake` (e.g., `live`, `sim`)

## Metrics and Logs
- Metrics: `snake_case` with unit suffix (e.g., `latency_ms`)
- Log fields: `snake_case` keys
