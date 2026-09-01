# Deployment Runbook — Production (THESHOW)

**This file is the source of truth for deploying to the production host.**
`docs/operations/deployment.md` covers standing a stack up from scratch
(local, Docker Compose, brokers, cloud); it does **not** describe production
deploys. `docs/operations/change-control.md` covers the paperwork and the
`make deploy-*` evidence tooling. When they disagree with this file, this
file wins.

Everything below is derived from deploys actually performed on this host, and
each law states the failure that produced it. None of it is theoretical.

---

## The host is not a git checkout

Read this before anything else, because it invalidates the obvious approach.

`/home/charl/subhft` on THESHOW is a **permanently divergent working tree**. It
is not, and must not be treated as, a checkout that can be fast-forwarded. As
of 2026-07-26 its HEAD is `3b1c10c8` while the tree carries 18 modified tracked
files in two distinct categories:

| Category | Files | Origin |
|---|---|---|
| **Live production edits** | `config/monitoring/alerts/rules.yaml`, `config/monitoring/prometheus.yml`, `config/symbols.list`, `config/symbols.yaml`, `docker-compose.yml`, `src/hft_platform/ops/backup.py` | Hand-edited on the host by the operator. **Exist nowhere else.** |
| **Deployed code** | `src/hft_platform/feed_adapter/shioaji/*`, `recorder/_loader_wal.py`, `recorder/wal.py`, `observability/metrics.py`, `services/system.py` | Copied in by past deploys, from commits that were never pushed |

`git pull`, `git checkout <sha>`, and `git reset --hard origin/main` each
destroy the first category irrecoverably. The old version of this runbook told
operators to run `git reset --hard origin/main` after stashing only
`symbols.list`, `symbols.yaml` and `contracts.json` — that pathspec misses
`rules.yaml`, `prometheus.yml`, `docker-compose.yml` and `ops/backup.py`.
Following it as written would have silently reverted four production edits.

**Deploy is therefore targeted file copy, never a git operation on the host.**

---

## Deploy Laws

Numbered so reviews and handoffs can cite them. A deploy that breaks one of
these is stopped, not argued about.

### D1 — Manual, and authorized per deploy

No automated push, no CI-triggered deploy, no agent deploying on its own
initiative. Authorization is per batch and does not carry forward from a
previous batch in the same session. A live-trading cutover
(`HFT_ORDER_MODE=live`) is a separate red line requiring its own explicit,
verbal decision — see `docs/runbooks/live-trading-activation-sop.md`.

### D2 — Never run git write operations on the host

No `pull`, `checkout`, `reset`, `stash`, `clean`, `merge`. Read-only git
(`status`, `log`, `show`, `diff`) is fine and is required by D3. See the
section above for why.

### D3 — Diff against what is deployed, not against local

Before overwriting any host file, fetch it and diff it against **the commit
that was last deployed**, not against your local working copy. Checksum
comparison against local only proves the copy will change something; it cannot
distinguish "my new code" from "I am about to revert the operator's live edit."

This law has paid for itself twice on the same file. Both the 2026-07-26 alert
deploy and the Phase E deploy would have reverted the deliberate removal of the
`market_trading_hours_active` guard on `FeedSubscriptionPermanentlyFailed` had
the file been copied wholesale. Both times the fix was the same: build a deploy
candidate that is *local changes applied on top of the host file*, then verify
the candidate-vs-host diff contains **only** the intended hunks.

```bash
# 1. fetch the host copy
scp charl@100.91.176.126:/home/charl/subhft/<path> /tmp/remote_<file>

# 2. prove the host has no edits you don't know about
git show <LAST_DEPLOYED_SHA>:<path> > /tmp/expected_<file>
diff -u /tmp/expected_<file> /tmp/remote_<file>      # empty => safe to overwrite

# 3. if NOT empty, the host has live edits. Build the candidate by hand and
#    diff it back against the host copy; it must show only your hunks.
diff -u /tmp/remote_<file> /tmp/candidate_<file>
```

`<LAST_DEPLOYED_SHA>` comes from the deploy ledger (D10).

### D4 — Back up every file you overwrite, before you overwrite it

Backups go to `~/deploy_backup_<UTC-date>/<batch-name>/`, preserving the repo
path structure, with an `MD5SUMS.txt`. Rollback is then "copy back and restart"
with no reconstruction step. Never overwrite a host file whose only copy is the
one you are about to destroy.

### D5 — The deploy class determines the mechanism

Pick the class first; it decides everything else. Getting this wrong is how you
lose the writable layer (D6).

| Class | What changed | Mechanism | Engine restart | Writable layer |
|---|---|---|---|---|
| **A — bind-mounted code/config** | anything under `src/`, `config/`, `scripts/` | `scp` + `stop` → wait 60 s → `start` (D6a) | yes (~60 s) | **preserved** |
| **B — monitoring rules** | `config/monitoring/alerts/*.yaml`, `prometheus.yml` | `scp` + Prometheus reload API | **no** | untouched |
| **C — env / compose / image** | `.env`, `docker-compose.yml`, Dockerfile, dependency pins | `docker compose up -d` = **recreate** | yes | **DESTROYED** — needs rescue plan first |

Class A is the common case *because* `src/` is bind-mounted: the engine runs
the host working tree, not the baked image. A code change needs no image
rebuild and no recreate.

Verified engine bind mounts (2026-07-26):

```
/home/charl/subhft/src     -> /app/src        (rw)
/home/charl/subhft/config  -> /app/config     (rw)
/home/charl/subhft/scripts -> /app/scripts    (rw)
/home/charl/subhft/data    -> /app/data       (rw)
/home/charl/subhft/.wal    -> /app/.wal       (rw)
/home/charl/subhft/.state  -> /app/.state     (rw)
/home/charl/subhft/certs   -> /app/certs      (ro)
/home/charl/subhft/backups/clickhouse -> /backups (rw)
/home/charl/subhft/.hft-runtime -> /var/run/hft (rw)
```

Anything **not** in that list lives in the container's writable layer and is
lost on recreate.

### D6a — the engine is stopped and started, never restarted

`docker compose restart hft-engine` is **refuted** for this engine, twice, on
2026-06-21 and 2026-06-22 (`.agent/memory/failed-attempts.md`). The container
comes back before the broker has released the previous login's five sessions,
so the reconnect takes a `451`, `order_client` fails, and the quote facades come
up **logged out and unsubscribed while `FeedState` still reads `CONNECTED`** —
a silent failure a restart-and-glance verification cannot see.

```
  BEFORE (refuted)                    AFTER (D6a)
  ----------------                    -----------
  docker compose restart              docker compose stop hft-engine
        |                                   |
        |  container up in ~10 s            |  wait 60 s   <- broker releases
        v                                   |               the 5 sessions
  broker still holds 5 sessions             v
        |                             docker compose start hft-engine
        X  451 Too Many Connections         |
           order_client fails               v
           facades logged out          login flags true, subscribed_count
           FeedState=CONNECTED (lies)  back at its pre-stop value
```

**Verify with `subscribed_count` and the per-facade login flags, never with
`FeedState`** — that is the field that masked it both times.

`restart` is still correct for sidecars that hold no broker session
(Prometheus, Grafana, the wal-loader).

### D6 — `restart` is not `up -d`

`docker compose restart` keeps the container; `docker compose up -d` (with or
without `--force-recreate`, and implicitly on any env or image change)
**replaces** it and discards the writable layer.

What is in the writable layer today: `/app/outputs` — 32 files including the
autonomy evidence directories `20260719`…`20260724`, `runtime_state.json` and
`contract_refresh_status.json` — plus `/app/shioaji.log`. `/app/outputs` is
**not** bind-mounted, so those files exist only inside the container.

Before any Class C deploy, rescue them first and confirm the copy:

```bash
docker cp hft-engine:/app/outputs ./outputs_container_$(date -u +%Y%m%dT%H%M%SZ)
find ./outputs_container_* -type f | wc -l      # must match the pre-copy count
```

Verify preservation around every Class A deploy — count before and after must
be identical:

```bash
docker exec hft-engine sh -c 'find /app/outputs -type f | wc -l'
```

### D7 — Validate on the host before restarting

The engine restarts into whatever you copied. A syntax error means a crash
loop, not a failed deploy. Validate in the container that will run it, using
its own interpreter:

```bash
# Class A — every changed module
docker exec hft-engine python -m py_compile /app/src/<...>.py ... && echo "py_compile OK"

# Class B — before any reload
docker exec prometheus promtool check rules /etc/prometheus/alerts/rules.yaml
# expect: SUCCESS: N rules found
```

Local `make lint` / `typecheck` / `discipline` / focused tests are a
precondition, not a substitute: they run against a different interpreter and a
different file tree.

### D8 — One incident, one restart

Batch every fix for a given incident into a single restart. Each restart is a
broker re-login for all four quote facades and a gap in the feed; serial
restarts multiply that for no benefit and make it impossible to attribute a
post-restart symptom to a specific change.

Corollary: if a fix is ready but its batch is not, it waits. The 2026-07-26
Phase E deploy carried the `conn_id` labelling commit that had been sitting
undeployed for exactly this reason.

### D9 — Pass criteria are named before the restart, and are metric-level

"Looks healthy" is not a verification. Write down, in advance, the specific
series and log lines that must change, including their expected direction. A
deploy is verified only when those named checks are re-read after the restart
and reported with their actual values.

Every deploy verifies the baseline set:

```bash
docker inspect hft-engine --format 'StartedAt={{.State.StartedAt}} Restarts={{.RestartCount}} Health={{.State.Health.Status}}'
docker compose ps                                     # 11/11 healthy
docker compose logs hft-engine --since 10m | grep -icE 'code: ?451|Too Many Connections'   # 0
docker compose logs hft-engine --since 10m | grep -iE '"level": ?"(error|critical)"'       # empty
curl -s localhost:9090/metrics | grep '^hft_quote_conn_subscribed_count'   # 4 facades x 74
curl -s localhost:9090/metrics | grep '^shioaji_thread_alive'              # per conn_id, all 1.0
```

Plus whatever is specific to the change. Grep for a bare error code with care —
`grep -c 451` matches timestamps and byte counts; use `code: ?451`.

### D10 — Record the deploy, or the next one is blind

Append one line to `~/deploy_ledger.tsv` on the host per batch. Without it,
D3 has no `<LAST_DEPLOYED_SHA>` to diff against and the next operator has to
reconstruct it from memory or session logs.

```
<utc_iso>	<commit_sha>	<class>	<n_files>	<restarted>	<backup_dir>	<summary>
```

```bash
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "<sha>" "A" "9" "yes" \
  "~/deploy_backup_20260726/phaseE" "<one-line summary>" \
  >> ~/deploy_ledger.tsv
```

The host tree's own `git status` cannot serve this purpose: the deployed files
show as plain `M`, with no record of which commit they came from.

---

## Target host

| Field | Value |
|---|---|
| SSH target | `charl@100.91.176.126` |
| Project root | `/home/charl/subhft` |
| Containers | 11 (`hft-engine`, `wal-loader`, `clickhouse`, `redis`, `prometheus`, `grafana`… ) |
| Engine metrics | `localhost:9090/metrics` |
| Prometheus API | `localhost:9091` (container 9090 → host 127.0.0.1:9091) |
| Alert rules | host `config/monitoring/alerts/` → container `/etc/prometheus/alerts/` |
| Order mode | `HFT_ORDER_MODE=sim`, `HFT_GATEWAY_ENABLED=0` — **no real money** |

Connectivity smoke test:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=8 charl@100.91.176.126 \
  'echo HOST=$(hostname); date -Iseconds; test -d /home/charl/subhft && echo PROJECT_OK'
```

---

## Pre-deploy checklist

```text
[ ] D1  authorization for THIS batch obtained
[ ] local gates green: make lint / typecheck / discipline / dependency-boundary
[ ] focused tests pass, and the regression test failed before the fix
[ ] commit exists locally; note the SHA        : ____________
[ ] last deployed SHA from ~/deploy_ledger.tsv : ____________
[ ] deploy class (A / B / C)                   : ____
[ ] D3 diff done for every file; live edits identified
[ ] pass criteria written down (D9)
[ ] market closed, or change is safe mid-session
[ ] disk >= 20% free: ssh ... 'df -h /home/charl/subhft'
```

Market hours are 08:45–13:45 and the night session 15:00–05:00 CST. A weekend
or a between-sessions window is the cheap time to restart; mid-session is not.

---

## Procedure — Class A (bind-mounted code)

The common path. Reference execution: 2026-07-26 Phase E, 9 files, ~10 s
restart, facades back in 20 s.

```bash
HOST=charl@100.91.176.126
ROOT=/home/charl/subhft
BATCH=~/deploy_backup_$(date -u +%Y%m%d)/<batch-name>
FILES="src/hft_platform/... ..."          # explicit list, no globs
```

**1. Confirm the local files are clean** — never deploy someone else's
uncommitted work by accident:

```bash
git status --short $FILES        # must be empty
```

**2. D3 diff every file against the last deployed SHA** (see D3 above). Resolve
any live edit into a hand-built candidate before continuing.

**3. D4 back up on the host:**

```bash
ssh $HOST "set -e; cd $ROOT; mkdir -p $BATCH
for f in $FILES; do mkdir -p \"$BATCH/\$(dirname \$f)\"; cp -p \"\$f\" \"$BATCH/\$f\"; done
md5sum \$(find $BATCH -type f | sort) > $BATCH/MD5SUMS.txt
find $BATCH -type f | wc -l"
```

**4. Copy, then verify by checksum** — `scp` reporting success is not proof the
right bytes landed:

```bash
for f in $FILES; do scp -q "$f" "$HOST:$ROOT/$f"; done
md5sum $FILES                                        # compare against:
ssh $HOST "cd $ROOT && md5sum $FILES"
```

**5. D7 validate in the container:**

```bash
ssh $HOST "docker exec hft-engine python -m py_compile $(printf '/app/%s ' $FILES) && echo 'py_compile OK'"
```

**6. Capture the baseline** for the metrics named in D9 — you cannot show a
metric changed without its before value.

**7. Stop, wait, start** (D6a — never `restart`, never `up -d`):

```bash
ssh $HOST "cd $ROOT
docker exec hft-engine sh -c 'find /app/outputs -type f | wc -l'   # before
docker compose stop hft-engine"
sleep 60                                   # the broker's 5-session release
ssh $HOST "cd $ROOT
docker compose start hft-engine
docker exec hft-engine sh -c 'find /app/outputs -type f | wc -l'   # must match"
```

**8. Verify** — poll for readiness rather than sleeping a fixed interval, then
re-read every D9 check plus the change-specific ones. Facades take ~20 s: four
staggered logins ≥ 2 s apart, 74 symbols each, zero 451.

Confirm the restart actually picked up the new source. `HFT_GIT_SHA` /
`build_info{git_sha}` describe the **image** (and therefore the SDK and deps —
see `shioaji-version-diff.md`), which a bind-mount deploy does not change. The
running source tree is reported separately:

```bash
ssh $HOST "docker logs hft-engine --since 10m 2>&1 | grep running_code_identity"
# code_sha must differ from the value logged by the previous boot; files= is a
# sanity check that the whole tree was walked, not a truncated mount.
```

`hft_build_info{code_sha}` carries the same value, so
`count by (code_sha) (hft_build_info) > 1` catches two engines running
different source from the same image.

**9. D10 append the ledger line.**

---

## Procedure — Class B (monitoring rules)

No engine restart, so this is the cheapest class and is safe mid-session.

```bash
# D3 + D4 as above, then:
scp config/monitoring/alerts/rules.yaml $HOST:$ROOT/config/monitoring/alerts/rules.yaml

ssh $HOST 'docker exec prometheus promtool check rules /etc/prometheus/alerts/rules.yaml'
# must print SUCCESS with the expected rule count before you reload

ssh $HOST 'curl -s -XPOST localhost:9091/-/reload -w "http=%{http_code}\n" -o /dev/null'
```

Verify the reload actually took, including rule health — a reload can return
200 and still leave a rule erroring:

```bash
ssh $HOST 'curl -s localhost:9091/api/v1/rules' | python3 -c "
import sys,json
rs=[r for g in json.load(sys.stdin)['data']['groups'] for r in g['rules']]
print('rules:', len(rs))
print('unhealthy:', [r['name'] for r in rs if r.get('health')!='ok'] or 'none')"
```

New PromQL can also be checked against the live server before it ships:

```bash
ssh $HOST "curl -s --get localhost:9091/api/v1/query --data-urlencode 'query=<expr>'"
```

A gauge-based alert deserves one extra thought before it ships: **a stalled
writer freezes a gauge, it does not make it absent.** An alert whose expression
can be satisfied by a frozen value is not a liveness signal. Pair it with a
counter on the publisher — that is what `QuotePoolMetricsPublisherStalled` does
for the per-facade gauges.

---

## Procedure — Class C (env / compose / image)

Rare, expensive, and the only class that destroys state. Requires a rescue plan
in writing before it starts.

Container environment is fixed at creation, so an `.env` change **cannot** be
applied by `restart` — this is why `HFT_WAL_MANIFEST_PATH` sat in `.env`
unapplied on 2026-07-25 while the running loader still reported it unset.
Confirm what the container actually sees rather than trusting the file:

```bash
docker exec hft-engine env | grep '^HFT_'
```

Sequence:

1. Rescue the writable layer (D6) and verify the file count.
2. Back up `.env` / `docker-compose.yml` to `~/deploy_backup_<date>/` (both
   carry live production edits — D2/D3 apply with full force).
3. Apply the edit **by append or targeted `sed`**, never by copying the local
   file over the host's.
4. `docker compose up -d --no-deps hft-engine`
5. Confirm the new env is live, restore any rescued `outputs/` content that
   downstream tooling needs, and run the full D9 set.

Before reaching for Class C, ask whether a Class A change achieves the same
thing. The WAL manifest fix was deliberately written so the code change alone
was sufficient and the env var became belt-and-braces — that turned a Class C
deploy into a Class A one.

---

## Ordering constraints

Some steps are order-dependent in ways that are not obvious and have each cost
a wasted restart.

**Re-arm before restart.** `hft ops rearm-platform` invoked via
`docker compose exec` runs in a *separate* process and cannot reach the live
controller — it logs `manual_rearm_ipc_unreachable` and only persists `false`
to the state file. The running engine never sees it. So re-arm must come
**before** the restart that picks it up, or you need a second restart.

**A non-auto-recoverable latch survives restarts.** `recorder_data_loss` is
deliberately non-auto-recoverable, and `_restore_manual_rearm_state` re-latches
reduce-only on boot. A bare restart does not clear it.

**Prometheus first, engine second.** When a batch contains both rules and code,
reload the rules first: it is reversible in seconds and independent, so a
failure there costs nothing. The restart is the irreversible half.

---

## ClickHouse migrations

Operator-driven, never automatic. Snapshot row counts, apply in filename order,
verify:

```bash
docker exec clickhouse clickhouse-client --query "
  SELECT 'market_data' AS t, count() FROM hft.market_data
  UNION ALL SELECT 'orders', count() FROM hft.orders
  UNION ALL SELECT 'fills',  count() FROM hft.fills"

ls src/hft_platform/migrations/clickhouse/ | sort | tail -25

for f in src/hft_platform/migrations/clickhouse/<NEW_DATE_PREFIX>*.sql; do
  echo "==> applying $f"
  docker exec -i clickhouse clickhouse-client --multiquery < "$f"
done
```

> `20260425_001_fills_replacing_merge_tree.sql` swaps the engine on `hft.fills`.
> Take a row-count and min/max-ts snapshot before and after.

Never rewrite an applied migration; append a new one. If one fails mid-batch,
inspect `system.query_log` — do not rerun blindly, and do not improvise a
reverse migration. Restore from backup if the state is unclear
(`docs/runbooks/clickhouse-down.md`).

Local ClickHouse has had its `market_data` TTL removed and therefore diverges
from the migration DDL; see `docs/operations/local-clickhouse-market-data-corpus.md`.

---

## Rollback

Rollback is defined by the deploy class, and every class has one because of D4.

**Class A / B** — copy the backup back and re-apply the mechanism:

```bash
ssh $HOST "set -e; cd $ROOT
B=~/deploy_backup_<date>/<batch>
cd \$B && md5sum -c MD5SUMS.txt          # backup integrity first
cd $ROOT
for f in \$(cd \$B && find . -type f ! -name MD5SUMS.txt | sed 's|^\./||'); do cp -p \"\$B/\$f\" \"\$f\"; done
docker compose stop hft-engine" && sleep 60 && ssh $HOST "cd $ROOT && docker compose start hft-engine"
# Class B: promtool + reload instead of the stop/start
```

**Class C** — restore `.env`/compose from the backup, then `up -d` again, then
restore the rescued writable-layer content.

Rollback is verified by the same D9 checks as the deploy. A rollback you did
not verify is a second untested change.

Rehearse it: `make rollback-drill` (`scripts/rollback_drill.py`) simulates the
procedure and verifies health restoration.

---

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| Engine crash-loops immediately after restart | syntax/import error in a copied file | D7 was skipped — roll back (D4), then `py_compile` before retrying |
| Config change has no effect after `restart` | it is an env var; container env is creation-time | Class C; confirm with `docker exec hft-engine env` |
| `/app/outputs` empty after deploy | `up -d` was used instead of `restart` | rescue from backup if one was taken; D6 exists to prevent this |
| Operator's live edit disappeared | file copied wholesale without a D3 diff | restore from `~/deploy_backup_*`, re-apply your hunks onto the host copy |
| Prometheus reload 200 but alert missing | rule file parsed, rule unhealthy | check `api/v1/rules` health, not just the HTTP code |
| `manual_rearm_required` still 1 after re-arm | `exec` ran in a separate process | re-arm **before** the restart; see Ordering constraints |
| Reduce-only re-latches on every boot | non-auto-recoverable `recorder_data_loss` | fix the underlying cause first, then re-arm, then restart |
| Broker `code: 451` after restart | concurrent logins across quote facades | login slot should serialize them; check `shioaji_login_slot_timeouts_total` and `docs/runbooks/feed-reconnect.md` |
| `grep -c 451` returns non-zero but no error | substring match in timestamps/values | use `code: ?451\|Too Many Connections` |

---

## Core dump capture (forensics setup)

The engine container ships with a `core: 4294967296` ulimit (4 GiB, one engine
memory image, matching `deploy.resources.limits.memory: 4G`) and a host bind
mount `./.cores → /var/cores`. Core capture also needs **host** kernel state,
which no image or compose file provisions:

```bash
echo '/var/cores/core.%e.%p.%t' | sudo tee /proc/sys/kernel/core_pattern
echo 'kernel.core_pattern=/var/cores/core.%e.%p.%t' | sudo tee /etc/sysctl.d/60-hft-cores.conf
sudo sysctl --system
```

> If the host runs `systemd-coredump`, `core_pattern` may be a
> `|/usr/lib/systemd/...` pipe, which captures into the journal
> (`coredumpctl list`) instead of the bind mount. Choose one; do not interleave.

Verify inside the container:

```bash
docker exec hft-engine sh -c 'ulimit -c; ls -ld /var/cores'
# expect 4194304 (KB — 4 GiB cap, NOT unlimited); drwxr-xr-x ... hftuser
```

`unlimited` means the cap did not deploy — the disk-fill cascade below is
unbounded until it is fixed. Sanity-check without touching the engine PID:

```bash
docker exec hft-engine sh -c '(sleep 1; kill -SEGV $$) & wait'
ls -lh ./.cores/
```

Debug against the same image so symbols line up:

```bash
CORE=$(ls -t .cores/core.* | head -1)
docker run --rm -it -v "$PWD/.cores:/var/cores:ro" \
  --entrypoint gdb hft-platform:latest \
  /usr/bin/python3 "/var/cores/$(basename "$CORE")"
```

A signature containing `pybind11::error_already_set` or originating in
`librust_core*.so` goes under `docs/incidents/` and to the rust_core maintainer.

### Core dump disk retention

The 4 GiB cap bounds one dump, but `restart: always` plus a segfault loop
writes N × 4 GiB into `./.cores` — the same filesystem as `./.wal` and
`./data`. WAL durability loses if that fills. Required host setup:

```bash
# pre-deploy disk gate
df -BG --output=avail /home/charl/subhft \
  | awk 'NR==2 && $1+0 < 20 { print "FAIL: <20G free"; exit 1 }'

# hourly retention cron, once per host
sudo tee /etc/cron.d/hft-cores-retention >/dev/null <<'CRON'
0 * * * * charl find /home/charl/subhft/.cores -type f -name 'core.*' -mmin +1440 -delete
CRON
sudo chmod 644 /etc/cron.d/hft-cores-retention
```

If a crash loop is in progress and disk is already low, do not wait for the
cron: `docker compose stop hft-engine`, rotate the cores out of the project
tree, then restart. Past two cores of the same signature, delete the older ones.

---

## Related

- `docs/operations/deployment.md` — stack bring-up (local / compose / brokers / cloud)
- `docs/operations/change-control.md` — change records and `make deploy-*` evidence tooling
- `docs/runbooks/daily-ops-checklist.md` — pre-open and post-close checks
- `docs/runbooks/live-trading-activation-sop.md` — the `HFT_ORDER_MODE=live` red line
- `docs/runbooks/feed-reconnect.md` — broker reconnect, 451 storms, stranded facades
- `docs/runbooks/WALDiskPressureCritical.md` — WAL disk pressure and the re-arm sequence
- `.agent/rules/41-deployment.md` — the compact agent-facing form of these laws
