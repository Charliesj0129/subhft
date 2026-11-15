# Dependency Audit SOP (Quarterly)

Last reviewed: 2026-11-15

## Schedule

Run quarterly (Q1/Q2/Q3/Q4 first week). Assign owner in the team rotation.

## 1. Python Dependency Audit

### 1.1 Run pip-audit

```bash
uv sync --locked --dev
make security-audit
```

The target exports the locked runtime, dev, research, bot and monitor dependencies
and runs `pip-audit --strict --no-deps --disable-pip`. Research packages need not
be installed to scan them. Any vulnerability or tool/export/network failure fails
the audit; there is no `pip check` fallback. Review `audit-output.txt` for the
result. The private Fubon SDK is not in the public lock and is not covered.

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
cargo install cargo-audit --locked  # first time only
cargo audit --file Cargo.lock
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

- [ ] Python `make security-audit` clean (no known vulnerabilities)
- [ ] Rust `cargo audit --file Cargo.lock` clean (no known vulnerabilities)
- [ ] Outdated packages reviewed and updated where safe
- [ ] CI passes on updated dependencies
- [ ] Benchmark shows no regression >5%
- [ ] Audit log entry created
- [ ] PR merged and deployed to sim for validation
