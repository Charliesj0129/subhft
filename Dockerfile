# syntax=docker/dockerfile:1.7

# Reproducibility contract (do not weaken without reading this):
#   * base image      pinned by digest (Dependabot `docker` bumps tag + digest)
#   * Python packages installed ONLY from a hashed requirements file that the
#     builder stage derives from uv.lock with `uv export --locked` (the build
#     fails if uv.lock is stale against pyproject.toml), and installed with
#     `--require-hashes`. uv.lock is the single source: there is no second
#     committed file that could drift from it.
#   * Rust crates     built with `--locked` against the committed Cargo.lock
#   * Rust toolchain  exact version, installer verified by sha256
#   * maturin         pinned with hashes (docker/build/requirements.txt)
# Not pinned: Debian packages from apt (bookworm security updates are wanted).

# Build stage
FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3 AS builder

WORKDIR /app

# Build toolchain for Rust extensions
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        build-essential \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Install a pinned Rust toolchain (needed for PyO3 extensions). The rustup
# installer is pinned by version and verified against a committed sha256 before
# it is executed; rustup then verifies the toolchain it downloads itself.
ARG RUSTUP_VERSION=1.28.2
ARG RUSTUP_SHA256_X86_64=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c
ARG RUSTUP_SHA256_AARCH64=e3853c5a252fca15252d07cb23a1bdd9377a8c6f3efa01531109281ae47f841c
ARG RUST_VERSION=1.91.1
RUN set -eux; \
    case "$(uname -m)" in \
        x86_64)  triple=x86_64-unknown-linux-gnu;  sha="${RUSTUP_SHA256_X86_64}" ;; \
        aarch64) triple=aarch64-unknown-linux-gnu; sha="${RUSTUP_SHA256_AARCH64}" ;; \
        *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/rustup-init \
        "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/${triple}/rustup-init"; \
    echo "${sha}  /tmp/rustup-init" | sha256sum -c -; \
    chmod +x /tmp/rustup-init; \
    /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain "${RUST_VERSION}"; \
    rm /tmp/rustup-init
ENV PATH="/root/.cargo/bin:${PATH}"
ENV CARGO_HOME=/root/.cargo \
    CARGO_TARGET_DIR=/app/.cargo-target

# Cargo network tuning for slow/unstable links
ENV CARGO_NET_RETRY=10 \
    CARGO_HTTP_TIMEOUT=600 \
    CARGO_HTTP_LOW_SPEED_LIMIT=1 \
    CARGO_HTTP_LOW_SPEED_TIME=120 \
    CARGO_REGISTRIES_CRATES_IO_PROTOCOL=sparse

# maturin and uv, pinned with hashes.
COPY docker/build/requirements.txt /tmp/build-requirements.txt
RUN pip install --no-cache-dir --require-hashes --timeout 600 --retries 10 \
        -r /tmp/build-requirements.txt

# Derive the hashed runtime requirements from uv.lock. `--locked` refuses a lock
# that does not match pyproject.toml. The bot (python-telegram-bot) and monitor
# (rich) extras are baked in; dev and research groups are not.
COPY pyproject.toml uv.lock ./
RUN uv export --locked --no-dev --no-emit-project --extra bot --extra monitor \
        --format requirements-txt --no-header -o /app/requirements.lock.txt

# The workspace root manifest carries [profile.release] (lto, codegen-units=1);
# without it the wheel would be built with different optimisation settings than
# `make build-rust`. Cargo.lock pins every crate.
COPY Cargo.toml Cargo.lock ./
COPY rust_core/ ./rust_core/

# Fetch and build in ONE RUN. They used to be two RUNs, with the fetch result
# held only in a cache mount and the build run offline: when the fetch layer was
# a build-cache hit but the cache mount had been pruned, the offline build found
# an empty registry and failed. One RUN cannot get out of step with itself.
RUN --mount=type=cache,target=/root/.cargo/registry \
    --mount=type=cache,target=/root/.cargo/git \
    --mount=type=cache,target=/app/.cargo-target \
    cargo fetch --locked --manifest-path rust_core/Cargo.toml \
    && maturin build --release --locked --manifest-path rust_core/Cargo.toml -o /tmp/wheels

# Runtime stage
FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3

WORKDIR /app

# P2-d (2026-04-27): bake commit SHA + build timestamp into the image so the
# startup banner can read them and Prometheus can expose
# `hft_build_info{git_sha,build_ts}=1` for image-drift detection across
# services. Use --build-arg from CI / Makefile or pass directly to `docker build`.
ARG GIT_SHA=unknown
ARG BUILD_TS=unknown
ENV HFT_GIT_SHA=${GIT_SHA} \
    HFT_BUILD_TS=${BUILD_TS}
RUN echo "${GIT_SHA}" > /app/REVISION && echo "${BUILD_TS}" > /app/BUILD_TS

# Create non-root user
RUN groupadd -g 1000 hftuser && useradd -m -u 1000 -g hftuser hftuser

# Install system dependencies (including libfaketime for simulation date spoofing)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libfaketime \
        curl \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# Default container timezone (override with TZ env if needed)
ENV TZ=Asia/Taipei

# Copy source code first
COPY pyproject.toml .
COPY --from=builder /app/requirements.lock.txt ./requirements.lock.txt
COPY --from=builder /tmp/wheels/*.whl /tmp/wheels/
COPY src/ ./src/
COPY config/ ./config/
COPY scripts/ ./scripts/

# Install dependencies into system python (in container) from the hashed lock.
# The lock already contains the bot (python-telegram-bot) and monitor (rich)
# extras and every transitive dependency, so nothing is resolved at build time:
# `--no-deps` makes pip install exactly the listed files and `--require-hashes`
# refuses any file whose sha256 is not in the lock. `pip check` fails the build
# if the installed set is internally inconsistent.
ENV PIP_DEFAULT_TIMEOUT=600
RUN pip install --no-cache-dir --timeout 600 --retries 10 \
        --require-hashes --no-deps --only-binary=:all: -r requirements.lock.txt \
    && pip check
# Install Rust extension wheel (fast-path helpers). Built above from Cargo.lock;
# its own dependencies are all in the lock, so no resolution happens here either.
RUN pip install --no-cache-dir --no-deps /tmp/wheels/*.whl && pip check

# Create directories for data/wal and set permissions
RUN mkdir -p .wal data && chown -R hftuser:hftuser /app

# P1 forensics: core-dump destination owned by hftuser. The host bind-mounts
# ./.cores here; pair with host `kernel.core_pattern=/var/cores/core.%e.%p.%t`.
RUN mkdir -p /var/cores && chown hftuser:hftuser /var/cores

# Switch to non-root user
USER hftuser

# Expose metrics port
EXPOSE 9090

# Set python path
ENV PYTHONPATH="${PYTHONPATH}:/app/src"
# Limit glibc malloc arenas to prevent multi-thread memory fragmentation.
# Default (8*cores) causes hundreds of MB of wasted anonymous segments.
ENV MALLOC_ARENA_MAX=2

# Entrypoint default (overridden by command)
CMD ["python", "-m", "hft_platform", "run"]
