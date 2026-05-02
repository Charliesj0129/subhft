#!/bin/bash
# ClickHouse init script: create hft_app user with least-privilege access.
# Placed in /docker-entrypoint-initdb.d/ via docker-compose volume mount.
# Uses env vars: HFT_CH_APP_USER, HFT_CH_APP_PASSWORD.

set -e

APP_USER="${HFT_CH_APP_USER:-hft_app}"
APP_PASSWORD="${HFT_CH_APP_PASSWORD:-${CLICKHOUSE_PASSWORD}}"

# Validate username: alphanumeric + underscore only (prevent SQL injection)
if ! [[ "${APP_USER}" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
    echo "ERROR: HFT_CH_APP_USER must be alphanumeric (got: '${APP_USER}')" >&2
    exit 1
fi

# Validate password: reject single quotes (prevent SQL injection via string escape)
if [[ "${APP_PASSWORD}" == *"'"* ]]; then
    echo "ERROR: HFT_CH_APP_PASSWORD must not contain single quotes" >&2
    exit 1
fi

CH_CMD="clickhouse-client --user default --password ${CLICKHOUSE_PASSWORD}"

${CH_CMD} --query "CREATE DATABASE IF NOT EXISTS hft"

${CH_CMD} --query \
    "CREATE USER IF NOT EXISTS ${APP_USER} IDENTIFIED BY '${APP_PASSWORD}' DEFAULT DATABASE hft"

${CH_CMD} --query \
    "GRANT CREATE DATABASE, CREATE TABLE, ALTER TABLE, DROP TABLE ON hft.* TO ${APP_USER}"

${CH_CMD} --query \
    "GRANT INSERT, SELECT ON hft.* TO ${APP_USER}"

${CH_CMD} --query \
    "GRANT CREATE VIEW, DROP VIEW ON hft.* TO ${APP_USER}"

echo "ClickHouse app user '${APP_USER}' initialized with least-privilege access to hft.*"
