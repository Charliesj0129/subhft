# Dependency security remediation, 2026-11-15

**2026-11-16 follow-up:** The inventory and test results below describe the
original remediation. Torch and stable-baselines3 were subsequently removed
from the research group and lock, which also removed their fsspec dependency.
The TorchScript and Torch ONNX exporter tests and the Torch-specific onnx-ir
constraint were removed; ONNX CPU inference remains covered separately. Alerts
for Torch and fsspec therefore require verification as removed dependencies,
not as upgrades. See the current lock and fresh scan results for current state.

[STATUS] Local dependency remediation, full local CI and research checks
verified. No push, merge, deployment or broker session is part of this change.

The implementation updates Python security floors in `pyproject.toml` and its
`uv.lock`, migrates existing Rust bindings to PyO3 0.29.3 / numpy 0.29.0, and
updates the workspace `Cargo.lock`. The Shioaji 1.7.11 and prometheus-client
0.24.1 locked versions are unchanged; the `<0.25` metrics constraint remains.

The scanner formerly omitted research/extras and accepted a missing scanner;
the Rust CI audit also referenced a nonexistent lock and ignored its failure:

```text
uv sync --dev -> pip-audit -> X research / bot / monitor omitted
missing pip-audit -> pip check -> X vulnerability audit never reached
rust_core/Cargo.lock -> X missing -> continue-on-error -> green CI

make security-audit -> uv export --locked -> complete public requirements
                    -> pip_audit --strict --no-deps --disable-pip
CI cargo audit -> Cargo.lock -> nonzero status blocks the job
```

`scripts/security_audit.py` audits the exported requirements without installing
the research or optional packages. The Makefile retains `audit-output.txt`,
including bootstrap/export/tool errors. Independent CI security steps run even
after another scan fails, retain their failing statuses, and summarize outcomes.
The Rust cache now uses workspace `target` and hashes both workspace manifests
and the root lock. Research CI installs the research group before its tests.

## GitHub alert coverage

The following 24 alerts were still open on the default branch at the time of
the read-only inventory. These rows describe the local repair; closure requires
updating the default branch and re-querying GitHub. No alert is ignored or
dismissed. Python rows are covered by the complete-lock audit; Rust rows by the
workspace audit and the binding compatibility checks.

| Alert | Severity | Package / manifest | Advisory | Local version |
|---|---|---|---|---|
| #1 | LOW | pyo3 / Cargo.lock | GHSA-pph8-gcv7-4qj5 | 0.21.2 -> 0.29.3 |
| #2 | MEDIUM | bytes / Cargo.lock | GHSA-434x-w66g-qw3r | 1.11.0 -> 1.11.1 |
| #3 | HIGH | pyo3 / Cargo.lock | GHSA-36hh-v3qg-5jq4 | 0.21.2 -> 0.29.3 |
| #4 | MEDIUM | pyo3 / Cargo.lock | GHSA-chgr-c6px-7xpp | 0.21.2 -> 0.29.3 |
| #5 | LOW | pyo3 / rust_core/Cargo.toml | GHSA-pph8-gcv7-4qj5 | 0.21.2 -> 0.29.3 |
| #6 | HIGH | pyo3 / rust_core/Cargo.toml | GHSA-36hh-v3qg-5jq4 | 0.21.2 -> 0.29.3 |
| #7 | MEDIUM | pyo3 / rust_core/Cargo.toml | GHSA-chgr-c6px-7xpp | 0.21.2 -> 0.29.3 |
| #8 | HIGH | onnx / uv.lock | GHSA-hqmj-h5c6-369m | 1.20.1 -> 1.22.0 |
| #9 | HIGH | onnx / uv.lock | GHSA-538c-55jv-c5g9 | 1.20.1 -> 1.22.0 |
| #10 | MEDIUM | onnx / uv.lock | GHSA-cmw6-hcpp-c6jp | 1.20.1 -> 1.22.0 |
| #11 | MEDIUM | onnx / uv.lock | GHSA-p433-9wv8-28xj | 1.20.1 -> 1.22.0 |
| #12 | HIGH | onnx / uv.lock | GHSA-q56x-g2fj-4rj6 | 1.20.1 -> 1.22.0 |
| #13 | HIGH | Mako / uv.lock | GHSA-v92g-xgxw-vvmm | 1.3.10 -> 1.4.2 |
| #14 | HIGH | Mako / uv.lock | GHSA-2h4p-vjrc-8xpq | 1.3.10 -> 1.4.2 |
| #15 | MEDIUM | onnx / uv.lock | GHSA-hwpq-hmq9-wj77 | 1.20.1 -> 1.22.0 |
| #16 | LOW | torch / uv.lock | GHSA-rrmf-rvhw-rf47 | 2.10.0 -> 2.13.0 |
| #18 | LOW | onnx / uv.lock | GHSA-p893-rvq9-2xf9 | 1.20.1 -> 1.22.0 |
| #21 | HIGH | fsspec / uv.lock | GHSA-27vj-qcqg-25rc | 2026.1.0 -> 2026.6.0 |
| #22 | MEDIUM | Mako / uv.lock | GHSA-5639-2j2p-m4mx | 1.3.10 -> 1.4.2 |
| #70 | MEDIUM | onnx / uv.lock | GHSA-48m4-24cq-37qf | 1.20.1 -> 1.22.0 |
| #71 | MEDIUM | onnx / uv.lock | GHSA-m4pr-6v3c-6fgr | 1.20.1 -> 1.22.0 |
| #72 | MEDIUM | onnx / uv.lock | GHSA-q69g-f4f5-v8m7 | 1.20.1 -> 1.22.0 |
| #73 | MEDIUM | onnx / uv.lock | GHSA-6pwm-x34p-fvj2 | 1.20.1 -> 1.22.0 |
| #74 | LOW | tornado / uv.lock | GHSA-5cjw-f85j-cv6r | 6.6 -> 6.6.1 |

Inventory command:

```bash
gh api 'repos/Charliesj0129/subhft/dependabot/alerts?state=open&per_page=100' \
  --jq '.[] | [.number,.dependency.package.ecosystem,.dependency.package.name,.dependency.manifest_path,.security_advisory.severity,.security_advisory.ghsa_id,.security_vulnerability.first_patched_version.identifier] | @tsv'
```

The updated RustSec database additionally reported the following issues during
the required audit; all were patched instead of adding ignores:

| Package | Advisory | Local version |
|---|---|---|
| crossbeam-epoch | [RUSTSEC-2026-0204](https://rustsec.org/advisories/RUSTSEC-2026-0204) | 0.9.18 -> 0.9.20 |
| memmap2 | [RUSTSEC-2026-0186](https://rustsec.org/advisories/RUSTSEC-2026-0186) | 0.7.1 -> 0.9.11 |
| rustix | [RUSTSEC-2026-0486](https://rustsec.org/advisories/RUSTSEC-2026-0486) | 1.1.5 -> 1.1.6 |
| tokio | [RUSTSEC-2026-0386](https://rustsec.org/advisories/RUSTSEC-2026-0386) | 1.50.0 -> 1.51.6 |

## Binding and research compatibility

The extension explicitly declares `gil_used = true`. Shared-memory types own
their `MmapMut` and derive pointers locally for each method borrow; cached raw
pointers and their manual `Send` implementations are removed. No `unsafe impl
Sync` is added. Mapping sizes use checked arithmetic and reject undersized
existing files; ring capacity zero is rejected before creating a file. The
single-writer shared-memory layout and existing seqlock protocol are unchanged.

Binding changes use fallible object conversion and `Py<PyAny>` ownership,
explicit `clone_ref(py)`, and `numpy::ndarray` types. A baseline release build
provided 171 public callable signatures; comparison with the migrated release
found zero differences. Tests cover tuple order, integer prices, boolean types,
NumPy shapes/dtypes, output ownership, overwritten object release, ring wrap,
zero/L1/L5 depth and reopened shared-memory mappings.

PyTorch's modern ONNX exporter initially failed with
`AttributeError: module 'onnx_ir' has no attribute 'schemas'` against the old
0.1.15 lock. The additional `onnx-ir>=0.1.16` floor and locked 0.1.16 restore
CPU/TorchScript/ONNX-export round trips. The test preserves modern
`dynamo=True` export. ONNX model construction, checking, save/load and CPU
inference are also covered by `tests/research/test_dependency_compatibility.py`.

## Validation

[VALIDATION] Commands run in the isolated feature worktree, with all public
groups installed using `uv sync --locked --dev --group research --extra bot
--extra monitor`. `UV_NO_SYNC=1` keeps that prepared environment during checks.

| Command | Observed result |
|---|---|
| `env UV_NO_SYNC=1 make security-audit` | exit 0; `No known vulnerabilities found` |
| `cargo audit --file Cargo.lock` | exit 0; 104 crate dependencies; no vulnerabilities or warnings |
| `cargo fmt --manifest-path rust_core/Cargo.toml -- --check` | exit 0 |
| `cargo clippy --locked --manifest-path rust_core/Cargo.toml -- -D warnings` | exit 0 |
| `cargo test --locked --manifest-path rust_core/Cargo.toml --no-default-features` | 131 passed, 0 failed |
| `env UV_NO_SYNC=1 make build-rust` | compiled module imported successfully |
| `env UV_NO_SYNC=1 uv run maturin develop --release --locked --manifest-path rust_core/Cargo.toml` | release module imported successfully |
| Required migration suite (command below) | 67 passed, 0 skipped; JUnit skipped count checked |
| `env UV_NO_SYNC=1 make feature-parity` | 56 frames, Python/Rust/hftbacktest comparisons all `ok: true` |
| `env UV_NO_SYNC=1 make perf-gate-feature-rust` | exit 0; no gate failures; Rust feature 10.937 us/event against unchanged 20 us limit; mismatch rate 0 |
| `env UV_NO_SYNC=1 make shioaji-guard` | 3 passed |
| `env UV_NO_SYNC=1 make check` | exit 0; 3 import contracts kept; 0 critical assertion violations; agent docs 0 errors |
| `env UV_NO_SYNC=1 PYTEST_XDIST_AUTO_NUM_WORKERS=8 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 make ci` | exit 0; 16854 passed, 16 skipped; coverage 88.22% (70% floor) |
| `env UV_NO_SYNC=1 COVERAGE_FILE=.agent/runtime/security-validation/research.coverage make research-check` | exit 0; 1879 passed, 2 skipped, 2 deselected; coverage 46.72% (38% floor) |

```bash
.venv/bin/python -m pytest --no-cov \
  tests/unit/test_rust_dependency_compatibility.py \
  tests/unit/test_security_audit.py \
  tests/research/test_dependency_compatibility.py \
  tests/unit/test_rust_hotpath_parity.py \
  tests/unit/test_rust_fast_lob.py tests/unit/test_typed_ring_parity.py \
  tests/unit/test_shm_snapshot.py tests/integration/test_monitor_shm_roundtrip.py \
  -q -ra --junitxml=.agent/runtime/security-validation/required-tests.xml
```

Regression evidence: restoring the old Makefile/workflow makes four scanner
tests fail; the fixed files were then restored byte-exact with SHA-256 checks.
Against a release build of the unmodified baseline, the new zero-capacity and
two short-mapping checks fail with `DID NOT RAISE ValueError`. They pass against
the rebuilt migrated module. The ONNX IR compatibility test failed before its
dependency floor was raised and passed after it. Independent review found the
missing research CI installation step; after that correction the re-review
verdict was APPROVE.

## Remaining scope

[NOT RUN] GPU execution and CUDA 13 driver/device compatibility: the local
PyTorch build is `2.13.0+cu130`, `torch.cuda.is_available()` returned `False`
and `torch.cuda.device_count()` returned 0. Also not run: GitHub Actions on this
feature branch; post-merge alert closure; remote/runtime deployment.
The private Fubon SDK is outside both the public lock and the Python audit.

[RISK] Torch's locked CUDA dependency family changes from 12 to 13. Local CPU
compatibility does not establish GPU compatibility. Existing scheduled CI was
already red for the audit and recorder performance; the local feature-Rust
performance gate passes without changing thresholds. No production artifacts,
live configuration, frozen research state, protected SDK/metrics versions or
goldens were changed. This is local remediation evidence, not release approval.

[VERDICT] Local remediation is verified and ready for review. HOLD for GPU use
until CUDA 13 is validated on the intended driver/device; publishing and
deployment remain separate operations.
