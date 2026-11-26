#!/usr/bin/env bash
# Decide whether the Darwin benchmark job is relevant. Unknown diffs always run.
set -u

# One list for both git diff and local --dry-run PATH... checks. Directory
# pathspecs include every descendant, including benchmark baselines/dotfiles.
# AGENTS.md and hft-hot-path-dev define the runtime scope; the additional
# dependencies are exercised by tests/benchmark and Makefile perf-gate-default.
benchmark_paths=(
  src/hft_platform/feed_adapter
  src/hft_platform/feature
  src/hft_platform/engine
  src/hft_platform/strategy
  src/hft_platform/strategies
  src/hft_platform/risk
  src/hft_platform/gateway
  src/hft_platform/order
  src/hft_platform/execution
  src/hft_platform/contracts
  src/hft_platform/core
  src/hft_platform/services
  src/hft_platform/events.py
  src/hft_platform/trade_classifier.py
  src/hft_platform/observability
  src/hft_platform/diagnostics/trace.py
  src/hft_platform/ipc
  src/hft_platform/utils
  src/hft_platform/ops/strategy_governor.py
  src/hft_platform/ops/session_governor.py
  src/hft_platform/ops/autonomy.py
  src/hft_platform/recorder
  src/hft_platform/backtest
  src/hft_platform/alpha/stats.py
  src/hft_platform/config/calibration_profiles.py
  src/hft_platform/infra/ch_client.py
  research/backtest
  research/combinatorial
  research/registry/schemas.py
  tests/benchmark
  tests/conftest.py
  scripts/benchmark_gate.py
  scripts/ci/benchmark_scope.sh
  Makefile
  .github/workflows/ci.yml
  .github/actions/setup-python-uv
  pyproject.toml
  uv.lock
)

decide() {
  local run="$1" reason="$2" label=RUN
  [[ "$run" == false ]] && label=SKIP
  printf '%s: %s\n' "$label" "$reason"
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf 'run=%s\n' "$run" >> "$GITHUB_OUTPUT"
  fi
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    printf '## Benchmark (Darwin Gate)\n\n%s: %s\n' \
      "$label" "$reason" >> "$GITHUB_STEP_SUMMARY"
  fi
}

if [[ "${1:-}" == --dry-run ]]; then
  shift
  for changed_path in "$@"; do
    for benchmark_path in "${benchmark_paths[@]}"; do
      if [[ "$changed_path" == "$benchmark_path" || "$changed_path" == "$benchmark_path/"* ]]; then
        decide true 'A supplied path touches the hot path or benchmark machinery.'
        exit 0
      fi
    done
  done
  decide false 'No supplied paths touch the hot path or benchmark machinery.'
  exit 0
fi

case "${CI_EVENT_NAME:-}" in
  push|pull_request) ;;
  *) decide true 'Event has no supported base/head diff; running conservatively.'; exit 0 ;;
esac

base="${CI_BASE_SHA:-}"
head="${CI_HEAD_SHA:-}"
for revision in "$base" "$head"; do
  if [[ ! "$revision" =~ ^[0-9a-fA-F]{40}$ || "$revision" =~ ^0+$ ]]; then
    decide true 'Base or head SHA is missing or invalid; running conservatively.'
    exit 0
  fi
done

ensure_commit() {
  git cat-file -e "$1^{commit}" 2>/dev/null || {
    git fetch --no-tags --depth=1 origin "$1" &&
      git cat-file -e "$1^{commit}" 2>/dev/null
  }
}

if ! ensure_commit "$base" || ! ensure_commit "$head"; then
  decide true 'Base or head commit could not be resolved; running conservatively.'
  exit 0
fi

# Compare the event endpoints directly, not HEAD^ (a push can contain many
# commits, and PR checkout HEAD can be a synthetic merge). Disable rename
# detection so moves out of the scope still count as scoped deletions.
if ! changed_paths=$(git diff --name-only --no-ext-diff --no-textconv --no-renames \
  "$base" "$head" -- "${benchmark_paths[@]}"); then
  decide true 'Git diff failed; running conservatively.'
elif [[ -n "$changed_paths" ]]; then
  decide true 'Changes touch the hot path or benchmark machinery.'
else
  decide false 'No changes touch the hot path or benchmark machinery; benchmarks skipped.'
fi
