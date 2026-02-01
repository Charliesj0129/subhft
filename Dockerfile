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
RUN curl https://sh.rustup.rs -sSf | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"

# Install uv for fast dependency management
RUN pip install uv

# Copy dependency files
# Copy dependency files
COPY pyproject.toml ./
COPY rust_core/ ./rust_core/
COPY src/ ./src/

# Generate requirements.txt from pyproject.toml (fresh resolve)
RUN uv pip compile pyproject.toml -o requirements.txt

# Build Rust extension wheel
RUN pip install maturin \
    && maturin build --release --manifest-path rust_core/Cargo.toml -o /tmp/wheels

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
    && rm -rf /var/lib/apt/lists/*

# Copy source code first (needed for -e . install)
COPY pyproject.toml .
COPY --from=builder /app/requirements.txt .
COPY --from=builder /tmp/wheels/*.whl /tmp/wheels/
COPY src/ ./src/
COPY config/ ./config/
COPY scripts/ ./scripts/

# Install dependencies into system python (in container)
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r requirements.txt
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
