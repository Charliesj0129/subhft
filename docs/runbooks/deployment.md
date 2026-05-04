# Deployment Runbook (Manual SSH)

## Overview

The HFT Platform is deployed manually over SSH. There is no scripted deploy
entrypoint — operators connect to the target host, build the Docker image
in-place, and restart the engine via `docker compose`. This is intentional:
remote configuration (`config/symbols.list`, `config/symbols.yaml`, etc.) is
operator-canonical and must not be overwritten by an automated push.

> **Live trading**: any deploy that ends in `HFT_MODE=live` /
> `HFT_ORDER_MODE=live` is a real-money change. Always verbalize the change
> to the responsible operator before running step 7.

## Target Host

| Field           | Value                          |
|-----------------|--------------------------------|
| SSH alias       | `THESHOW`                      |
| SSH target      | `charl@100.91.176.126`         |
| Project root    | `/home/charl/subhft`           |
| Compose service | `hft-engine` (+ deps)          |

Connectivity smoke test:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=8 charl@100.91.176.126 \
  'echo HOST=$(hostname); date -Iseconds; test -d /home/charl/subhft && echo PROJECT_OK'
```

## Pre-Deploy Checklist

- [ ] Push-trigger CI is **green** on the target SHA on `main`
      (`gh run list --branch main --workflow CI --limit 5`).
- [ ] Market is closed, or you are deploying outside TSE/TAIFEX session
      hours (09:00–13:30 UTC+8). Sunday is the safest window.
- [ ] Operator with broker access is reachable (Shioaji session may need
      manual re-login after engine restart).
- [ ] Disk on remote ≥ 20 % free
      (`ssh charl@... 'df -h /home/charl/subhft'`).
- [ ] You have a recent ClickHouse backup or know where the daily backup is
      written (see `docs/runbooks/clickhouse-down.md`).

## Deploy Steps

All commands run **on the remote** unless explicitly noted. Substitute
`<TARGET_SHA>` with the SHA you intend to ship (e.g. tip of `origin/main`).

### 1. Connect and inspect current state

```bash
ssh charl@100.91.176.126
cd /home/charl/subhft
git rev-parse --short HEAD
git status --short
docker compose ps
curl -fsS http://localhost:9090/metrics | \
  grep -E '^(build_info|hft_strategy_position_current|feed_events_total)' | head -10
```

Note the running image SHA, the strategies loaded, and any open
positions. **Save the previous image SHA — that's your rollback target.**

```bash
PREV_IMAGE_SHA=$(docker inspect hft-engine --format '{{.Image}}')
echo "$PREV_IMAGE_SHA" > /tmp/hft-rollback-image.txt
```

### 2. Stash operator-canonical config drift

The remote `config/` directory is the source of truth for runtime
configuration. Stash it before pulling so a fast-forward merge can't
clobber it.

```bash
git stash push -u -m "operator-config-drift-$(date -u +%Y%m%dT%H%M%SZ)" -- \
  config/symbols.list config/symbols.yaml \
  'config/symbols.list.bak.*' 'config/symbols.yaml.bak.*' \
  'config/contracts.json.bak.*'
```

If `git status --short` is clean afterwards, proceed. If files remain
modified, inspect them — they may not match the stash pathspec.

### 3. Fetch and check out the target SHA

```bash
git fetch origin
git checkout <TARGET_SHA>      # or `git reset --hard origin/main` for tip-of-main
git rev-parse --short HEAD
```

### 4. Restore operator config drift on top

```bash
git stash pop
git status --short
```

If `git stash pop` reports a conflict in `config/symbols.list` or
`config/symbols.yaml`, resolve manually: the remote operator copy wins
unless the upstream change is critical. Re-add and re-commit nothing —
these stay out of git.

### 5. Build the image with provenance metadata

```bash
GIT_SHA=$(git rev-parse HEAD)
BUILD_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

docker build \
  --build-arg "GIT_SHA=${GIT_SHA}" \
  --build-arg "BUILD_TS=${BUILD_TS}" \
  -t "hft-platform:${GIT_SHA:0:8}" \
  -t hft-platform:latest \
  .
```

Confirm both tags exist:

```bash
docker images hft-platform --format '{{.Tag}}\t{{.ID}}\t{{.CreatedSince}}' | head
```

### 6. Apply ClickHouse migrations

Migrations are operator-driven. Take a quick row-count snapshot, then
apply each new SQL file in order:

```bash
docker exec clickhouse clickhouse-client --query "
  SELECT 'market_data' AS t, count() FROM hft.market_data
  UNION ALL SELECT 'orders',  count() FROM hft.orders
  UNION ALL SELECT 'fills',   count() FROM hft.fills"

ls src/hft_platform/migrations/clickhouse/ | sort | tail -25

# Apply only files newer than what's already on this host:
for f in src/hft_platform/migrations/clickhouse/<NEW_DATE_PREFIX>*.sql; do
  echo "==> applying $f"
  docker exec -i clickhouse clickhouse-client --multiquery < "$f"
done
```

> ⚠️ `20260425_001_fills_replacing_merge_tree.sql` swaps the engine on
> `hft.fills`. Take a row-count + min/max-ts snapshot of that table
> before/after, and confirm before continuing.

### 7. Flip mode (operator decision)

Skip this step if you want to keep the existing `.env`. Otherwise back up
the file first and edit explicitly:

```bash
cp .env .env.bak.$(date -u +%Y%m%dT%H%M%SZ)

# Sim / shadow:
sed -i 's/^HFT_MODE=.*/HFT_MODE=sim/'                        .env
sed -i 's/^HFT_ORDER_MODE=.*/HFT_ORDER_MODE=sim/'            .env
sed -i 's/^HFT_ORDER_SHADOW_MODE=.*/HFT_ORDER_SHADOW_MODE=1/' .env
sed -i '/^HFT_LIVE_CONFIRM=/d'                               .env

# Live (real money):
sed -i 's/^HFT_MODE=.*/HFT_MODE=live/'                       .env
sed -i 's/^HFT_ORDER_MODE=.*/HFT_ORDER_MODE=live/'           .env
sed -i 's/^HFT_ORDER_SHADOW_MODE=.*/HFT_ORDER_SHADOW_MODE=0/' .env
grep -qE '^HFT_LIVE_CONFIRM=' .env \
  && sed -i 's/^HFT_LIVE_CONFIRM=.*/HFT_LIVE_CONFIRM=yes-i-know/' .env \
  || echo 'HFT_LIVE_CONFIRM=yes-i-know' >> .env
```

> **Live confirmation must be verbal.** Run the live branch only after
> the responsible operator acknowledges the impact.

### 8. Restart the engine with the new image

```bash
docker compose up -d --no-deps --force-recreate hft-engine
sleep 8
docker compose ps hft-engine
```

### 9. Health check

```bash
curl -fsS http://localhost:9090/metrics | \
  grep -E '^(build_info|hft_strategy_position_current|feed_events_total|hft_recorder_writes_total|hft_order_)' | head -20
docker logs --tail=120 hft-engine 2>&1 | tail -60
```

Pass criteria:

- `build_info{git_sha="<TARGET_SHA short>",build_ts="<ISO>"} 1` —
  confirms the new image is live (no more `unknown`).
- `feed_events_total` increasing during market hours; static at 0 when
  market is closed (Sunday) is expected.
- `hft_strategy_position_current{strategy_id=...}` lines present for
  every enabled strategy.
- No `ERROR`-level log lines about ClickHouse connectivity, Rust kernel
  ABI mismatch, or migration drift.

If any check fails, jump to **Rollback**.

### 10. Post-deploy observation (≥ 10 min during a session)

- `hft_recorder_writes_total` non-zero and increasing.
- ClickHouse: `SELECT max(toDateTime64(exch_ts/1e9,3)) FROM hft.market_data`
  is fresh.
- WAL directory not growing unexpectedly: `ls -lh .wal/ | head`.
- Watch `docker compose logs -f hft-engine` for at least one full minute.

## Rollback

```bash
ssh charl@100.91.176.126
cd /home/charl/subhft

PREV=$(cat /tmp/hft-rollback-image.txt)
docker tag "$PREV" hft-platform:latest
docker compose up -d --no-deps --force-recreate hft-engine
docker compose ps hft-engine
curl -fsS http://localhost:9090/metrics | grep build_info
```

If a migration must be undone, restore from the latest ClickHouse backup
(`docs/runbooks/clickhouse-down.md`). Do not improvise reverse-migrations.

## Troubleshooting

| Symptom                                      | Likely cause                            | Action                                                                                       |
|----------------------------------------------|-----------------------------------------|----------------------------------------------------------------------------------------------|
| `build_info{git_sha="unknown"}`              | Image built without `--build-arg`       | Rebuild with both `GIT_SHA` and `BUILD_TS` (step 5).                                         |
| Stash pop conflict in `config/`              | Upstream changed the same file          | Hand-merge; remote operator value wins unless explicitly overridden.                         |
| `docker compose up` recreates the wrong tag  | Stale `HFT_IMAGE` env override          | `unset HFT_IMAGE` and re-run step 8.                                                         |
| Strategies still empty after restart         | `config/strategies.yaml` not loaded     | Confirm working tree matches operator expectation; check `docker logs` for "loading strategy". |
| Shioaji login fails on first restart         | Broker session expired                  | Operator re-runs the manual login flow; engine retries on next bootstrap.                    |
| ClickHouse migration partially applied       | One SQL file failed mid-batch           | Inspect `system.query_log`; do not rerun blindly. Restore from backup if state is unclear.   |

## Pre-Deploy Checklist (copy-paste)

```text
[ ] CI green on TARGET_SHA  : ____________________
[ ] Market closed / off-hours
[ ] Operator on standby
[ ] Disk ≥ 20 % free
[ ] CH backup recent
[ ] PREV_IMAGE_SHA recorded : ____________________
[ ] TARGET_SHA              : ____________________
[ ] New CH migrations to apply count : ____
[ ] Mode after deploy (sim / live)   : ____
```

## Core Dump Capture (Forensics Setup)

The engine container ships with `core: 4294967296` ulimit (4 GiB cap, one
engine memory image — matches `deploy.resources.limits.memory: 4G`) on the
`*hft-common` anchor and a host bind mount at `./.cores → /var/cores`
(same anchor). The cap bounds the disk-fill cascade documented in **Disk
usage / retention** below. To make core capture effective, the **host**
kernel core-pattern sysctl must point at the bound directory; this is
host-side state that is not provisioned by the image or by
`docker compose`.

One-time setup on the deploy host:

```bash
# Active session (effective immediately):
echo '/var/cores/core.%e.%p.%t' | sudo tee /proc/sys/kernel/core_pattern

# Persist across reboots:
echo 'kernel.core_pattern=/var/cores/core.%e.%p.%t' | \
  sudo tee /etc/sysctl.d/60-hft-cores.conf
sudo sysctl --system
```

> If the host runs `systemd-coredump`, `core_pattern` may be set to a
> `|/usr/lib/systemd/...` pipe. That captures cores into the journal
> (`coredumpctl list`) instead of the bind mount — choose one path; do
> not interleave the two.

Verify inside the container after `docker compose up -d hft-engine`:

```bash
docker exec hft-engine sh -c 'ulimit -c; ls -ld /var/cores'
# Expected: 4194304 (kilobytes — 4 GiB cap, NOT unlimited);
#           drwxr-xr-x ... hftuser hftuser ...
```

If `ulimit -c` shows `unlimited`, the cap fix did not deploy — investigate
before proceeding (the disk-fill cascade in **Disk usage / retention**
below is no longer bounded).

Sanity check that **does not touch the engine PID** — spawn a throwaway
shell and SIGSEGV it:

```bash
docker exec hft-engine sh -c '(sleep 1; kill -SEGV $$) & wait'
ls -lh ./.cores/
# Expected: a fresh core.<comm>.<pid>.<ts> file appears within seconds.
```

After a real crash, debug with gdb against the same image so symbols line
up:

```bash
CORE=$(ls -t .cores/core.* | head -1)
docker run --rm -it -v "$PWD/.cores:/var/cores:ro" \
  --entrypoint gdb hft-platform:latest \
  /usr/bin/python3 "/var/cores/$(basename "$CORE")"
# (gdb) py-bt   # if python3-dbg available; otherwise: bt
```

If the crash signature contains `pybind11::error_already_set` or
originates from `librust_core*.so`, file the dump under `docs/incidents/`
and follow up with the rust_core maintainer.

### Disk usage / retention

`core: 4294967296` (4 GiB) caps a single dump at one engine memory image,
but with `restart: always` a segfault loop can still write **N × 4 GiB**
of cores into `./.cores` — which sits on the same filesystem as `./.wal`
and `./data`. WAL durability and ClickHouse ingestion lose if that disk
fills. The cap bounds each dump; the cron below bounds the accumulation.

**Required** operator setup (deploy-time, not optional):

```bash
# 1. Pre-deploy disk budget gate — MUST pass before bringing the engine up:
df -BG --output=avail /home/charl/subhft \
  | awk 'NR==2 && $1+0 < 20 { print "FAIL: <20G free"; exit 1 }'

# 2. Enforced hourly retention cron — install once per host (HIGH-2 fix,
#    Codex review 2026-05-04). Deletes core files older than 24h.
sudo tee /etc/cron.d/hft-cores-retention >/dev/null <<'CRON'
0 * * * * charl find /home/charl/subhft/.cores -type f -name 'core.*' -mmin +1440 -delete
CRON
sudo chmod 644 /etc/cron.d/hft-cores-retention

# Verify:
sudo cat /etc/cron.d/hft-cores-retention
```

Optional defense in depth (only after the cron above is in place):

```bash
# Hard cap by directory size — keep N most-recent cores, drop the rest.
# Run from a pre-restart hook if a crash loop is in progress and the
# hourly cron's <=60 min latency is too slow.
ls -1t /home/charl/subhft/.cores/core.* 2>/dev/null | tail -n +6 | xargs -r rm --
```

Add a Prometheus alert on `node_filesystem_avail_bytes` for the project
mount; page the operator before the device hits 5 GB free. Once you have
two cores of the same signature, delete the older ones — they rarely add
diagnostic value past the second occurrence.

If a crash loop is in progress and disk is already low, **do not** wait
for retention to catch up. Stop the engine (`docker compose stop
hft-engine`), rotate the cores out of the project tree manually, then
restart.
