#!/usr/bin/env bash
# deploy.sh — Manual deploy script for hft-platform.
#
# Usage:
#   ./scripts/deploy.sh                  # Deploy current HEAD
#   ./scripts/deploy.sh --dry-run        # Build and verify, do not push/deploy
#   ./scripts/deploy.sh --rollback <sha> # Rollback to a specific image tag
#
# Required environment variables (unless --dry-run):
#   DEPLOY_HOST   — Target host SSH address
#   DEPLOY_USER   — SSH user on target host
#   DEPLOY_KEY    — Path to SSH private key file
#   GHCR_TOKEN    — GitHub Container Registry token (for docker login)
#
# Optional:
#   REGISTRY      — Container registry (default: ghcr.io)
#   IMAGE_NAME    — Image name (default: auto-detected from git remote)

set -euo pipefail

# ── Constants ─────────────────────────────────────────────────────────────

REGISTRY="${REGISTRY:-ghcr.io}"
HEALTH_RETRIES=3
HEALTH_INTERVAL=10
REMOTE_DIR="/opt/hft-platform"

# ── Argument parsing ──────────────────────────────────────────────────────

DRY_RUN=false
ROLLBACK_SHA=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --rollback)
            ROLLBACK_SHA="${2:?'--rollback requires a git SHA argument'}"
            shift 2
            ;;
        -h|--help)
            head -14 "$0" | tail -13
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# ── Resolve image name ───────────────────────────────────────────────────

if [[ -z "${IMAGE_NAME:-}" ]]; then
    REMOTE_URL=$(git remote get-url origin 2>/dev/null || echo "")
    if [[ -n "$REMOTE_URL" ]]; then
        # Extract owner/repo from git URL
        REPO_PATH=$(echo "$REMOTE_URL" | sed -E 's#.*github\.com[:/](.+?)(\.git)?$#\1#')
        IMAGE_NAME="${REPO_PATH}/hft-engine"
    else
        echo "ERROR: Cannot determine IMAGE_NAME from git remote. Set IMAGE_NAME env var." >&2
        exit 1
    fi
fi

FULL_IMAGE="${REGISTRY}/${IMAGE_NAME}"

# ── Resolve deploy SHA ───────────────────────────────────────────────────

if [[ -n "$ROLLBACK_SHA" ]]; then
    DEPLOY_SHA="$ROLLBACK_SHA"
    echo "==> ROLLBACK mode: deploying image tag ${DEPLOY_SHA}"
else
    DEPLOY_SHA=$(git rev-parse HEAD)
    echo "==> DEPLOY mode: building and deploying ${DEPLOY_SHA}"
fi

IMAGE_TAG="${FULL_IMAGE}:${DEPLOY_SHA}"

# ── Build ─────────────────────────────────────────────────────────────────

echo "==> Building Docker image: ${IMAGE_TAG}"
docker build -t "${IMAGE_TAG}" -t "${FULL_IMAGE}:latest" .

if [[ "$DRY_RUN" == "true" ]]; then
    echo "==> DRY RUN: Image built successfully. Skipping push and deploy."
    echo "    Image: ${IMAGE_TAG}"
    exit 0
fi

# ── Push ──────────────────────────────────────────────────────────────────

if [[ -n "${GHCR_TOKEN:-}" ]]; then
    echo "==> Logging in to ${REGISTRY}"
    echo "${GHCR_TOKEN}" | docker login "${REGISTRY}" -u "$(git config user.name || echo deploy)" --password-stdin
fi

echo "==> Pushing ${IMAGE_TAG}"
docker push "${IMAGE_TAG}"
docker push "${FULL_IMAGE}:latest"

# ── Deploy ────────────────────────────────────────────────────────────────

if [[ -z "${DEPLOY_HOST:-}" ]] || [[ -z "${DEPLOY_USER:-}" ]] || [[ -z "${DEPLOY_KEY:-}" ]]; then
    echo "WARN: DEPLOY_HOST/DEPLOY_USER/DEPLOY_KEY not set. Image pushed but not deployed."
    echo "      Image: ${IMAGE_TAG}"
    exit 0
fi

echo "==> Deploying to ${DEPLOY_USER}@${DEPLOY_HOST}"

SSH_OPTS="-i ${DEPLOY_KEY} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"

# Pull and restart
# shellcheck disable=SC2029
ssh ${SSH_OPTS} "${DEPLOY_USER}@${DEPLOY_HOST}" \
    "cd ${REMOTE_DIR} && \
     docker pull ${IMAGE_TAG} && \
     docker compose -f docker-compose.yml up -d --no-deps hft-engine"

# ── Health check ──────────────────────────────────────────────────────────

echo "==> Running health checks (${HEALTH_RETRIES} attempts, ${HEALTH_INTERVAL}s interval)"

for i in $(seq 1 "${HEALTH_RETRIES}"); do
    sleep "${HEALTH_INTERVAL}"
    # shellcheck disable=SC2029
    if ssh ${SSH_OPTS} "${DEPLOY_USER}@${DEPLOY_HOST}" \
        "curl -sf http://localhost:9090/metrics | grep -q hft_"; then
        echo "==> Health check PASSED (attempt ${i}/${HEALTH_RETRIES})"
        echo "==> Deploy complete: ${IMAGE_TAG}"
        exit 0
    fi
    echo "    Health check attempt ${i}/${HEALTH_RETRIES} failed"
done

# ── Rollback on failure ──────────────────────────────────────────────────

echo "ERROR: Health check failed after ${HEALTH_RETRIES} attempts." >&2
echo "==> Attempting rollback (restarting previous container)..."

# shellcheck disable=SC2029
ssh ${SSH_OPTS} "${DEPLOY_USER}@${DEPLOY_HOST}" \
    "cd ${REMOTE_DIR} && docker compose -f docker-compose.yml up -d --no-deps hft-engine" || true

echo "ERROR: Deploy FAILED. System rolled back to previous state." >&2
echo "       Verify manually: ssh ${DEPLOY_USER}@${DEPLOY_HOST} 'docker ps'" >&2
exit 1
