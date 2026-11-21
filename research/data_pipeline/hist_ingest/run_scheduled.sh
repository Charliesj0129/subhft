#!/usr/bin/env bash
# Scheduled historical futures ingest (cron, Asia/Taipei). One job per broker, each under its own flock so a slow run
# never overlaps the next and at most one Shioaji session of this tool is open (broker cap: 5 per person_id).
#   fubon-kbars    Fubon 1-min K, 60 req/min (tool uses 50), no daily volume cap: full history from 2017-06.
#   shioaji-ticks  Shioaji ticks, 2 GB/day volume (resets 08:00 on trading days), 50 req/10 s, no day-session
#                  queries: rotate products in rounds of ROUND_DAYS until the quota floor stops a round.
set -euo pipefail
JOB="${1:?usage: run_scheduled.sh fubon-kbars|shioaji-ticks}"
WT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
ENV_FILE="${HIST_ENV_FILE:-/home/charlie/hft_platform/.env}"
PY="$WT/.venv/bin/python"
LOG_DIR="${HIST_LOG_DIR:-$HOME/hft_data/hist_ingest/logs}"
ROUND_DAYS="${HIST_ROUND_DAYS:-20}"
MAX_ROUNDS="${HIST_MAX_ROUNDS:-40}"
MIN_REMAINING_MB="${HIST_MIN_REMAINING_MB:-500}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$JOB-$(date +%Y%m%d).log"
exec 9>"$LOG_DIR/.$JOB.lock"
flock -n 9 || { echo "$(date -Is) $JOB already running" >>"$LOG"; exit 0; }
cd "$WT"
[ -f "$HOME/.claude/bin/ck-env.sh" ] && . "$HOME/.claude/bin/ck-env.sh" 2>/dev/null
docker ps --format '{{.Names}}' | grep -qx clickhouse || { docker start clickhouse >/dev/null && sleep 10; }

ingest() { "$PY" -m research.data_pipeline.hist_ingest "$@" --env-file "$ENV_FILE" 2>&1 | grep -v -E 'Response Code|Info:' | tee -a "$LOG"; }

echo "$(date -Is) start $JOB" >>"$LOG"
case "$JOB" in
  fubon-kbars)
    ingest kbars --source fubon --products TXF,MXF --since 2017-06-01
    ingest kbars --source fubon --products TMF --since 2024-07-29
    ;;
  shioaji-ticks)
    for round in $(seq 1 "$MAX_ROUNDS"); do
      progressed=0
      for spec in TXF:2020-03-23 MXF:2020-03-23 TMF:2024-07-29; do
        out="$(ingest ticks --products "${spec%%:*}" --since "${spec#*:}" --max-tick-days "$ROUND_DAYS" \
          --min-remaining-mb "$MIN_REMAINING_MB")" || true
        grep -q 'PENDING_QUOTA\|intraday\|refus' <<<"$out" && { echo "$(date -Is) stop: quota/session" >>"$LOG"; exit 0; }
        grep -q 'calls=[1-9]' <<<"$out" && progressed=1
      done
      [ "$progressed" = 1 ] || break
    done
    ;;
  *) echo "unknown job $JOB" >&2; exit 2 ;;
esac
echo "$(date -Is) end $JOB" >>"$LOG"
