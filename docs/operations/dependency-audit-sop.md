# Dependency Audit SOP (Quarterly)

Last reviewed: 2026-03-18

## Schedule

Run quarterly (Q1/Q2/Q3/Q4 first week). Assign owner in the team rotation.

## 1. Python Dependency Audit

### 1.1 Run pip-audit

```bash
uv run pip-audit --strict --desc
```

Review output for known CVEs. Any CRITICAL or HIGH severity finding blocks release.

### 1.2 Check for outdated packages

```bash
uv pip list --outdated
```

Prioritize updates for packages with known security advisories.

### 1.3 Lock file integrity

```bash
uv lock --check
```

Ensure `uv.lock` is consistent with `pyproject.toml`.

## 2. Rust Dependency Audit

### 2.1 Install and run cargo-audit

```bash
cargo install cargo-audit  # first time only
cargo audit --file rust_core/Cargo.lock
```

### 2.2 Check for outdated crates

```bash
cargo install cargo-outdated  # first time only
cargo outdated --manifest-path rust_core/Cargo.toml
```

### 2.3 Review unsafe usage

```bash
cargo install cargo-geiger  # first time only
cargo geiger --manifest-path rust_core/Cargo.toml
```

## 3. Version Update Procedure

1. Create branch: `chore/quarterly-dep-audit-YYYY-QN`
2. Update dependencies one group at a time (runtime, dev, build)
3. Run full CI suite after each group: `make ci`
4. Run integration tests: `make test-all`
5. Run benchmark to verify no performance regression: `make benchmark-ci`
6. Open PR with audit summary in description

### Update order (lowest risk first)

1. Dev/test dependencies (ruff, pytest, mypy)
2. Observability (prometheus-client, structlog)
3. Data layer (clickhouse-connect, numpy)
4. Broker SDK (shioaji) -- test in sim mode before merging
5. Rust crates -- rebuild extension and run parity tests

## 4. CVE Escalation Process

| Severity | SLA          | Action                                      |
|----------|--------------|---------------------------------------------|
| CRITICAL | 24 hours     | Hotfix branch, patch, deploy immediately    |
| HIGH     | 3 business days | Schedule patch in current sprint          |
| MEDIUM   | Next quarter | Include in next quarterly audit cycle       |
| LOW      | Best effort  | Track in backlog, update when convenient    |

### Escalation steps

1. Identify affected component and blast radius
2. Check if vulnerability is reachable in our code paths
3. If reachable: apply patch or pin to fixed version immediately
4. If not reachable: document rationale, schedule update per SLA
5. Notify team lead for CRITICAL/HIGH via team channel
6. Record finding in `docs/operations/audit-log.md` (create if absent)

## 5. Audit Checklist

- [ ] Python `pip-audit` clean (no CRITICAL/HIGH)
- [ ] Rust `cargo audit` clean (no CRITICAL/HIGH)
- [ ] Outdated packages reviewed and updated where safe
- [ ] CI passes on updated dependencies
- [ ] Benchmark shows no regression >5%
- [ ] Audit log entry created
- [ ] PR merged and deployed to sim for validation
