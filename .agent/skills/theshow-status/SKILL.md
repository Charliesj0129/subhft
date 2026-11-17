---
name: theshow-status
description: "Read-only snapshot of the production host THESHOW: containers, restarts, disk, firing and recent alerts, ERROR/WARNING log classes, risk-state hints, deploy ledger tail. Use for 遠端主機/正式機狀況、部屬狀況、告警通知查看、bot 發 warning. Haiku-safe. Not for deploys, restarts, fixes, or root cause (bug-investigation)."
---

# THESHOW status (read-only)

Haiku can run this end to end. It changes nothing on the host.

## Run

Use the literal command (an allow rule only matches a literal `ssh theshow`):

```bash
ssh theshow 'bash -s' < .agent/skills/theshow-status/scripts/host_snapshot.sh
```

Wider window: `ssh theshow 'HOURS=48 bash -s' < .agent/skills/theshow-status/scripts/host_snapshot.sh`

The script redacts account-ID-shaped tokens. Never paste `.env` content,
credentials, or account IDs into the reply.

## Report (Traditional Chinese, one screen)

| 項目 | 值 | 判讀 |
|---|---|---|
| hft-engine | status / StartedAt / RestartCount | RestartCount 增加 = 有重啟，要升級處理 |
| 磁碟 | 使用% | >85% 標記 |
| 告警 firing | 名稱 | none = OK |
| 近 N 小時告警 | 名稱與樣本數 | |
| ERROR/WARN | 前 5 類與次數 | 只列類別，不貼整段 log |
| ledger 最後部署 | 日期 + 說明 | |

End with one line: `[VERDICT] 正常 / 需注意 / 異常`.

## Escalate (hand back to a stronger model)

- A container down, RestartCount rose, HALT / REDUCE_ONLY / StormGuard not NORMAL,
  disk >90%, or an error class you cannot explain from this page.
- The user asks why (use `bug-investigation`) or asks to act: deploy, restart and
  kill switch are red lines (`.agent/rules/41-deployment.md`).

## Traps

- `HFT_ORDER_MODE=sim` still dispatches orders; do not call sim "safe".
- Alerts at session open or close are often session edges, not incidents.
- A metric reading 0 may never have been set: write 未確認, not 正常.
- Never run `docker compose restart` or `up -d`, even when asked casually.
