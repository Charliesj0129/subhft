# syntax=docker/dockerfile:1.7

# Build stage
FROM python:3.11-slim-bookworm as builder

WORKDIR /app

# Build toolchain for Rust extensions
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        build-essential \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Install Rust toolchain (needed for PyO3 extensions)
# Download and verify rustup installer
RUN curl -sSf https://sh.rustup.rs -o /tmp/rustup-init.sh \
    && echo "Verifying rustup installer..." \
    && sh /tmp/rustup-init.sh -y --default-toolchain stable \
    && rm /tmp/rustup-init.sh
ENV PATH="/root/.cargo/bin:${PATH}"
ENV CARGO_HOME=/root/.cargo \
    CARGO_TARGET_DIR=/app/.cargo-target

# Copy dependency manifests first for better Docker layer caching
COPY pyproject.toml ./
RUN mkdir -p rust_core
COPY rust_core/Cargo.toml ./rust_core/Cargo.toml
COPY rust_core/src/lib.rs ./rust_core/src/lib.rs

# Generate a simple runtime requirements.txt from pyproject dependencies.
# This avoids an extra `uv` bootstrap download in constrained build environments.
RUN python - <<'PY'
import tomllib
from pathlib import Path

data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
deps = data.get("project", {}).get("dependencies", [])
Path("requirements.txt").write_text("".join(f"{dep}\n" for dep in deps), encoding="utf-8")
PY

# Cargo network tuning for slow/unstable links
ENV CARGO_NET_RETRY=10 \
    CARGO_HTTP_TIMEOUT=600 \
    CARGO_HTTP_LOW_SPEED_LIMIT=1 \
    CARGO_HTTP_LOW_SPEED_TIME=120 \
    CARGO_REGISTRIES_CRATES_IO_PROTOCOL=sparse

# Install maturin and prefetch Rust dependencies using cache mounts.
# We keep this explicit prefetch step so progress is visible in Docker logs,
# then run `maturin build` in offline mode to avoid hanging-looking cargo
# metadata/index updates during the build step.
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/root/.cargo/registry \
    --mount=type=cache,target=/root/.cargo/git \
    pip install --no-cache-dir --timeout 600 --retries 10 maturin \
    && cargo fetch --manifest-path rust_core/Cargo.toml -v

# Copy remaining sources after toolchain/dependency prefetch so code changes
# don't invalidate the cargo prefetch layer.
COPY rust_core/ ./rust_core/
COPY src/ ./src/

# Build Rust extension wheel using warm caches and offline cargo mode.
# This avoids a second network/index roundtrip inside `maturin`.
RUN --mount=type=cache,target=/root/.cargo/registry \
    --mount=type=cache,target=/root/.cargo/git \
    --mount=type=cache,target=/app/.cargo-target \
    CARGO_NET_OFFLINE=true maturin build --release --manifest-path rust_core/Cargo.toml -o /tmp/wheels

# Runtime stage
FROM python:3.11-slim-bookworm

WORKDIR /app

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

# Copy source code first (needed for -e . install)
COPY pyproject.toml .
COPY --from=builder /app/requirements.txt .
COPY --from=builder /tmp/wheels/*.whl /tmp/wheels/
COPY src/ ./src/
COPY config/ ./config/
COPY scripts/ ./scripts/

# Install dependencies into system python (in container)
ENV PIP_DEFAULT_TIMEOUT=600
RUN pip install --no-cache-dir --timeout 600 --retries 10 -r requirements.txt
# Install Rust extension wheel (fast-path helpers)
RUN pip install --no-cache-dir /tmp/wheels/*.whl

# Create directories for data/wal and set permissions
RUN mkdir -p .wal data && chown -R hftuser:hftuser /app

# Switch to non-root user
USER hftuser

# Expose metrics port
EXPOSE 9090

# Set python path
ENV PYTHONPATH="${PYTHONPATH}:/app/src"

# Entrypoint default (overridden by command)
CMD ["python", "-m", "hft_platform", "run"]
