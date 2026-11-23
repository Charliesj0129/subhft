# Free-threaded engine image (CPython 3.14t)

An opt-in image that runs the hft-engine on free-threaded CPython 3.14t (GIL off). It is
built from its own file, `Dockerfile.engine-py314t`, and is **not** wired into any compose
file. Nothing here changes what runs in production today.

Status as of 2026-11-23: the image has **not** been built end to end (no Docker daemon was
available where it was written). The hashed lock install and `scripts/py314t/check_gil.py`
were run outside Docker against CPython 3.14.0t and passed (76 modules, GIL stayed off).
The first real `docker build` is still to do; see "Not yet verified".

## What is in the repo

| File | Role |
| --- | --- |
| `Dockerfile.engine-py314t` | The image: pinned base, uv-managed 3.14t, hashed lock install, GIL smoke step |
| `requirements/runtime-py314t.in` | Input list for the 3.14t runtime set (derived from `pyproject.toml`, which is not edited) |
| `requirements/runtime-py314t.lock` | uv pip compile output, hashes for every file, 30 packages |
| `scripts/py314t/check_gil.py` | Fails when an import re-enables the GIL; used at build time and in CI |
| `.github/workflows/py314t-runtime.yml` | Non-blocking CI job, see "CI job" |

The existing `Dockerfile`, `docker-compose.yml`, `docker-compose.production.yml` and
`docker-compose.prod.locked.yml` are untouched. The research, wal-loader, monitor and bot images
keep being built from `Dockerfile`.

## Build

```
docker build -f Dockerfile.engine-py314t -t hft-engine:py314t-local \
  --build-arg "GIT_SHA=$(git rev-parse HEAD)" .
```

Build arguments:

| Arg | Default | Meaning |
| --- | --- | --- |
| `PYTHON_314T_VERSION` | `3.14.10` | Exact free-threaded patch release fetched by uv |
| `REQUIRE_GH159449_FIX` | `0` | `1` fails the build when the interpreter is older than 3.14.11 |
| `GIT_SHA`, `BUILD_TS` | `unknown` | Baked in as in the `Dockerfile` (startup banner, build-info metric) |

The build needs network access for apt, pip (hashed build tools) and the uv interpreter
download. The build fails when any of these happens: a lock hash does not match, a package has
no wheel, `uv pip check` finds an inconsistent set, or `check_gil.py` exits non-zero.

Check the result:

```
docker run --rm hft-engine:py314t-local python /app/scripts/py314t/check_gil.py
```

Exit codes of `check_gil.py`: 0 clean; 1 an import re-enabled the GIL (it names the first one);
2 the GIL is still off but some module failed to import; 3 not a free-threaded build, or
`PYTHON_GIL` / `-X gil` forces the GIL. Never pass `-e PYTHON_GIL=0` when checking: forcing the
GIL off hides exactly the failure the script looks for, so the script refuses (exit 3).

## What it pins

| Layer | Pin | Where |
| --- | --- | --- |
| Base image | `python:3.12-slim-bookworm@sha256:54c85f3c...`, the same digest as `Dockerfile` (its 3.12 interpreter is unused; it supplies Debian bookworm userland and pip) | `Dockerfile.engine-py314t` |
| Interpreter | uv-managed CPython 3.14t, exact patch in `PYTHON_314T_VERSION` | `Dockerfile.engine-py314t` |
| uv | `uv==0.13.4` with hashes, installed with `--require-hashes` | `docker/build/requirements.txt` |
| Python packages | 30 packages with hashes, installed with `--require-hashes --only-binary :all: --no-deps` | `requirements/runtime-py314t.lock` |
| Broker SDK | `shioaji==1.7.11` | lock (kept from `pyproject.toml`) |
| Metrics client | `prometheus-client` 0.24.1 (`prometheus_client<0.25` kept) | lock |
| GIL | no `PYTHON_GIL` anywhere in the image | `Dockerfile.engine-py314t` |

Direct runtime packages in the lock: aiohttp 3.14.5, clickhouse-connect 1.13.0,
exchange-calendars 4.13.3, msgspec 0.22.0, numba 0.68.1, numpy 2.5.5, pandas 3.1.1,
prometheus-client 0.24.1, psutil 7.2.2, pyyaml 6.0.4, scipy 1.18.1, shioaji 1.7.11,
structlog 26.1.0, uvloop 0.23.0 (uvloop is listed explicitly because `shioaji[speed]` only
pulls it for Python below 3.14).

Runtime user is `hftuser` (uid/gid 1000, same as `Dockerfile`). `libfaketime`, `curl` and
`tzdata` are installed as in `Dockerfile`; there is no compiler toolchain.

### Interpreter patch level and gh-159449

gh-159449 is a free-threaded asyncio task-list race that can segfault the process. It is fixed in
3.14.11 and later. The newest 3.14t patch known when the file was written is **3.14.10**, so the
default `PYTHON_314T_VERSION` predates the fix. The build prints a warning in that case and
`REQUIRE_GH159449_FIX=1` turns it into a failure. Do not run this image against live orders until
the pin is 3.14.11 or newer.

To bump the interpreter: raise `PYTHON_314T_VERSION`, and if `uv python install` reports no
download for it, raise the `uv` pin in `docker/build/requirements.txt` first (a uv release can only
fetch interpreters it knows). Check what a given uv knows with `uv python list 3.14t --all-versions`.
The CI job installs the newest 3.14t its own uv knows, so its patch level can differ from the image.

## What it excludes

| Excluded | Why | Effect |
| --- | --- | --- |
| `hftbacktest` | No cp314t x86_64 wheel | `strategies/mm_hawkes.py` imports it unguarded, so that strategy is research-only on 3.14t. Three more imports (`backtest/adapter.py`, `backtest/convert.py`) sit inside `try` blocks |
| `orjson` | No cp314t wheel | 18 import sites in 14 files (execution, gateway, order, recorder, observability, monitor, services) are all inside `try` blocks, so the modules import, but the fallback behaviour is not exercised here. The planned fix replaces orjson with msgspec, on branch `perf/p2-orjson-to-msgspec-20261123` (not part of this change). Until it lands, treat the engine on this image as not ready to take orders |
| Rust extension (maturin wheel) | The image has no Rust toolchain and builds no wheel | Imports are guarded; `check_gil.py` passes without it. Rust work is paused, see `AGENTS.md` |
| research, dev, monitor, bot extras | Not engine dependencies (torch, optuna, rich, telegram and similar) | Those images keep using `Dockerfile` |

`check_gil.py` prints the full list of excluded-dependency imports with file and line, and whether
each one is inside a `try` block, so the counts above can be re-read at any time.

## CI job

`.github/workflows/py314t-runtime.yml` runs on pull requests that touch `src/**`,
`requirements/**`, `scripts/py314t/**` or the workflow itself, and on manual dispatch. It installs
uv, then the newest 3.14t, then the lock (same flags as the image) and `pytest`, runs
`check_gil.py`, and runs the 3.14t-safe test files that exist: `test_ft_shared_state.py`,
`test_jsonfast.py`, `test_md_loop_ledger*.py` and `test_gc_pause_bench.py`. It does not build the
image. The job sets `continue-on-error: true`, so it never blocks a merge yet.

To make it required:

1. Remove `continue-on-error: true` from the job, in a separate change, after the job has stayed
   green on main for a while.
2. Add the check named by the job (`Free-threaded 3.14t GIL check`) to the required status checks
   in branch protection for main.
3. Remove the `paths:` filter from the `pull_request` trigger. A required check skipped by a path
   filter stays pending and blocks every pull request that does not touch those paths.

## Rollback

Nothing replaces the existing image, so there is nothing to revert in the repository. The
engine keeps running from the image built by `Dockerfile` unless a deployment explicitly points the
engine service at the 3.14t image. To go back, point the engine at the existing image tag again and
follow `docs/runbooks/deployment.md` (per-batch authorization, backup, stop, wait 60 s, start). Do
not hot-swap images on the production host.

## Not yet verified

- A full `docker build` of `Dockerfile.engine-py314t` and `check_gil.py` inside the built image.
- That the pinned uv (0.13.4) can fetch the pinned `PYTHON_314T_VERSION`.
- TLS to the brokers and ClickHouse from the uv-managed interpreter's certificate store.
- Any run of the engine itself (orders, WAL, recorder) on 3.14t.
