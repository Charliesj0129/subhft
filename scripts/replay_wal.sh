#!/usr/bin/env bash
set -euo pipefail

# Replay archived WAL files by moving them back into .wal and restarting wal-loader.
# Assumes docker compose service named "wal-loader".

ARCHIVE_DIR="${ARCHIVE_DIR:-.wal/archive}"
WAL_DIR="${WAL_DIR:-.wal}"

if [ ! -d "$ARCHIVE_DIR" ]; then
  echo "Archive dir $ARCHIVE_DIR not found."
  exit 1
fi

echo "Stopping wal-loader..."
docker compose stop wal-loader || true

echo "Replaying archived WAL from $ARCHIVE_DIR -> $WAL_DIR"
mkdir -p "$WAL_DIR"
find "$ARCHIVE_DIR" -type f -name "*.jsonl" -maxdepth 1 -print -exec mv {} "$WAL_DIR"/ \;

echo "Starting wal-loader..."
docker compose start wal-loader

echo "Done."
