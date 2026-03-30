#!/usr/bin/env bash
# host_preflight.sh — Pre-deployment baseline verification.
# Runs on the REMOTE host. Exit 0 = all pass, 1 = any fail.
# Usage: bash scripts/host_preflight.sh
#
# Environment:
#   DEPLOY_ROOT  — project root (default: /home/charl/subhft)

set -euo pipefail
source "$(dirname "$0")/_notify.sh"

cd "$DEPLOY_ROOT"

errors=0
warnings=0

pass()  { echo "  ✓ $1"; }
warn()  { echo "  ⚠ $1"; warnings=$((warnings + 1)); }
fail()  { echo "  ✗ $1"; errors=$((errors + 1)); }

echo "=== HFT Host Preflight Check ==="
echo "    Host: $(hostname)"
echo "    Root: ${DEPLOY_ROOT}"
echo "    Time: $(date -Iseconds)"
echo ""

# 1. Docker version >= 24.0
echo "[1/8] Docker version"
if command -v docker &> /dev/null; then
    DOCKER_VER=$(docker --version | grep -oP '\d+\.\d+' | head -1)
    DOCKER_MAJOR=$(echo "$DOCKER_VER" | cut -d. -f1)
    if [ "$DOCKER_MAJOR" -ge 24 ]; then
        pass "Docker ${DOCKER_VER}"
    else
        fail "Docker ${DOCKER_VER} < 24.0"
    fi
else
    fail "Docker not found"
fi

# 2. Docker Compose
echo "[2/8] Docker Compose"
if docker compose version &> /dev/null; then
    COMPOSE_VER=$(docker compose version --short 2>/dev/null || docker compose version | grep -oP '\d+\.\d+\.\d+' | head -1)
    pass "Docker Compose ${COMPOSE_VER}"
else
    fail "Docker Compose not found"
fi

# 3. Disk available >= 20%
echo "[3/8] Disk space"
DISK_USED=$(df --output=pcent / | tail -1 | tr -d ' %')
DISK_AVAIL=$((100 - DISK_USED))
if [ "$DISK_AVAIL" -ge 20 ]; then
    pass "Disk ${DISK_AVAIL}% available"
else
    fail "Disk ${DISK_AVAIL}% available (< 20%)"
fi

# 4. Memory available >= 512 MB
echo "[4/8] Memory"
MEM_AVAIL=$(free -m | awk '/^Mem:/ {print $7}')
if [ "$MEM_AVAIL" -ge 512 ]; then
    pass "Memory ${MEM_AVAIL} MB available"
else
    fail "Memory ${MEM_AVAIL} MB available (< 512 MB)"
fi

# 5. Required ports not hijacked
echo "[5/8] Required ports"
for port in 8123 9090 6379; do
    if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
        pass "Port ${port} in use (expected)"
    else
        warn "Port ${port} not listening (service may be down)"
    fi
done

# 6. .env validation
echo "[6/8] .env validation"
if bash scripts/validate_env.sh > /dev/null 2>&1; then
    pass ".env validation passed"
else
    fail ".env validation failed (run scripts/validate_env.sh for details)"
fi

# 7. Docker services health
echo "[7/8] Docker services"
UNHEALTHY=$(docker compose ps --format '{{.Name}} {{.Status}}' 2>/dev/null | grep -ivE 'up|running' | head -5 || echo "")
if [ -z "$UNHEALTHY" ]; then
    SERVICE_COUNT=$(docker compose ps --format '{{.Name}}' 2>/dev/null | wc -l)
    pass "All ${SERVICE_COUNT} services healthy"
else
    fail "Unhealthy services: ${UNHEALTHY}"
fi

# 8. Core sysctl values (from ops.sh tune)
echo "[8/8] Sysctl tuning"
SYSCTL_OK=true
check_sysctl() {
    local key="$1" expected="$2"
    local actual
    actual=$(sysctl -n "$key" 2>/dev/null || echo "MISSING")
    if [ "$actual" != "$expected" ]; then
        warn "sysctl ${key}=${actual} (expected ${expected})"
        SYSCTL_OK=false
    fi
}
check_sysctl "net.core.rmem_max" "134217728"
check_sysctl "net.core.wmem_max" "134217728"
check_sysctl "net.core.somaxconn" "4096"
if [ "$SYSCTL_OK" = true ]; then
    pass "Sysctl tuning OK"
fi

# Summary
echo ""
echo "=== Preflight Summary ==="
if [ "$errors" -gt 0 ]; then
    echo "FAILED: ${errors} error(s), ${warnings} warning(s)"
    exit 1
else
    if [ "$warnings" -gt 0 ]; then
        echo "PASSED with ${warnings} warning(s)"
    else
        echo "PASSED: all checks OK"
    fi
    exit 0
fi
