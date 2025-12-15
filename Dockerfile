# Build stage
FROM python:3.12-slim-bookworm as builder

WORKDIR /app

# Install uv for fast dependency management
RUN pip install uv

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Generate requirements.txt from uv.lock
RUN uv export --format requirements-txt --no-hashes > requirements.txt

# Runtime stage
FROM python:3.12-slim-bookworm

WORKDIR /app

# Create non-root user
RUN groupadd -g 1000 hftuser && useradd -m -u 1000 -g hftuser hftuser

# Install system dependencies (if any needed for Shioaji/contracts)
# RUN apt-get update && apt-get install -y --no-install-recommends ...

# Copy source code first (needed for -e . install)
COPY pyproject.toml .
COPY --from=builder /app/requirements.txt .
COPY src/ ./src/
COPY config/ ./config/
COPY scripts/ ./scripts/

# Install dependencies into system python (in container)
RUN pip install --no-cache-dir -r requirements.txt

# Create directories for data/wal and set permissions
RUN mkdir -p .wal data && chown -R hftuser:hftuser /app

# Switch to non-root user
USER hftuser

# Expose metrics port
EXPOSE 9090

# Set python path
ENV PYTHONPATH="${PYTHONPATH}:/app/src"

# Entrypoint default (overridden by command)
CMD ["python", "-m", "hft_platform.main"]
