# Testing Lessons (test-INFRA traps and patterns)

Record here: test-infrastructure traps (gates, fixtures, modes, timeouts) and
fixture patterns that work. Do NOT record: individual test cases; product
bugs (those go to module_gotchas.md / failed-attempts.md).

## Single-file runs must bypass the repo coverage gate
`pytest tests/unit/test_x.py` alone FAILS the global `--cov-fail-under=70`
gate and looks like a test failure. Use `make test-file FILE=...` /
`make test-node NODE=...` (they add `--no-cov`).

## Global pytest timeout is 30s
`addopts` includes `--timeout=30`. Slow integration paths need explicit
markers/overrides, not sleeps. Fixed sleeps are banned; if unavoidable,
<=50 ms with an explanation.

## Event mode must be forced under test (2025-01)
Production event mode spawns background threads and network connections →
intermittent test failures. Force the test/sim event mode in fixtures.

## mypy needs explicit src-layout config (2025-01)
`mypy_path=["src"]`, `explicit_package_bases=true`, explicit `files` in
pyproject. Never rely on auto-discovery in this layout.

## Test hygiene is gated, not advisory
`make test-hygiene-check` enforces: every test asserts, behavior-oriented
names (`test_<behavior>_<scenario>`), no tautological patterns. Write to the
gate from the start.

## research/ is excluded from repo gates
ruff and pytest exclude `research/` (`tests/research` ignored). A green
`make ci` says nothing about research code — run research tests explicitly.

## Coverage gates must match reality (2025-01)
Set gates at actual current coverage and ratchet up; aspirational gates just
break CI. Repo gate 70%; targets: new code >=80%, hot path >=90%.
