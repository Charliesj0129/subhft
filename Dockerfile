# syntax=docker/dockerfile:1.7

# Reproducibility contract (do not weaken without reading this):
#   * base image      pinned by digest (Dependabot `docker` bumps tag + digest)
#   * Python packages installed ONLY from a hashed requirements file that the
#     builder stage derives from uv.lock with `uv export --locked` (the build
#     fails if uv.lock is stale against pyproject.toml), and installed with
#     `--require-hashes`. uv.lock is the single source: there is no second
#     committed file that could drift from it.
#   * build tools    pinned with hashes (docker/build/requirements.txt)
# Not pinned: Debian packages from apt (bookworm security updates are wanted).

# Build stage
FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3 AS builder

WORKDIR /app

# uv and Hatchling, pinned with hashes.
COPY docker/build/requirements.txt /tmp/build-requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: --timeout 600 --retries 10 \
        -r /tmp/build-requirements.txt

# Derive the hashed runtime requirements from uv.lock. `--locked` refuses a lock
# that does not match pyproject.toml. The bot (python-telegram-bot) and monitor
# (rich) extras are baked in; dev and research groups are not.
COPY pyproject.toml uv.lock ./
RUN uv export --locked --no-dev --no-emit-project --extra bot --extra monitor \
        --format requirements-txt --no-header -o /app/requirements.lock.txt

# Build the same Python wheel used by local installs, including the hft CLI.
COPY src/ ./src/
RUN python -m hatchling build --target wheel --directory /tmp/wheels

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
# Install the application wheel without re-resolving its locked dependencies.
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
