# AGENTS.md - Testing Domain

> **Context**: This context is injected when working within `tests/`.
> **Inheritance**: Inherits global laws from `../AGENTS.md`.

## 1. Test Isolation rules
- **No External Network**: Unit tests must NEVER hit real APIs.
    - **Use**: `pytest-mock`, `respx`, or local Docker containers.
- **No Global Mutation**: Tests must not modify global interpreter state without restoration.

## 2. Async Testing
- **Framework**: Use `pytest-asyncio`.
- **Marker**: Decorate async tests with `@pytest.mark.asyncio`.

## 3. Data Fixtures
- **Generators**: Use `hypothesis` for property-based testing where possible.
- **Snapshots**: Store large expected outputs in `tests/data/`, not inline strings.

## 4. Performance Tests
- **Separation**: Mark heavy/slow tests with `@pytest.mark.slow` or `@pytest.mark.benchmark`.
- **Tools**: Use `pytest-benchmark` for latency assertions.
