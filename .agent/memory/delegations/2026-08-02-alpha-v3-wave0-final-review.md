# Alpha Mining v3 Wave 0 — independent final review

## Packet

Independent read-only Tier-3 final code review of Alpha Mining v3 Wave 0.

Goal/task/tier: adversarially review the current diff vs 28f37adb in /tmp/hft-alpha-mining-v3-wave0. Tier-3 alpha governance; no implementation. Determine whether locked-validation block permutation, exact feature-provenance walk-forward embargo, finite-family metadata, synthetic controls, campaign fail-closed artifact, resume serialization, and SMMA warning are correct and safe.

Branch/status: alpha/mining-v3-validation-wave0-20260802, modified source/tests/memory, no commits. Main worktree dirty and off-limits. Expected relevant files are the current git diff only.

Permissions: READ ONLY. No edits, no git state changes, no artifact writes, no network. Read governing CLAUDE.md/AGENTS.md/rules and actual source/diff. Read tests. Read existing immutable campaign artifacts only if useful. You may run focused pytest/ruff with caches in /tmp; do not run live data or ClickHouse.

Specific checks: array-axis alignment and NaN/exchangeability semantics; permutation statistic validity and p-value; decision=e-1 provenance/off-by-one/coarse 60m projection/reset semantics; SMMA recursive history handling; inability to bypass exact-history API; DSR/BH control semantics; false positives under intraday seasonality; deterministic artifact hash including code fingerprint and resume mismatch; CLI stops before legs; old checkpoint defaults/tuple round trip; no final holdout access; no relaxed statistical floors.

Constraints/gotchas: user existing run dirs immutable; no frozen/live/profile changes; controls are conditional harness evidence, not alpha evidence. General repo rules override this enumeration. Stop/escalate on need to write or conflicting instructions.

Verification: cite file:line and command evidence. If running tests, failures outside diff are reported only. Do not fix.

Output: findings ranked severity with concrete failure scenarios and rule; explicit APPROVE / APPROVE-WITH-NITS / REQUEST-CHANGES / ESCALATE; commands run and checks not run. Budget bounded to final review.

## Executor final report

## Findings

無剩餘已確認的 Tier‑3 blocker。

已在審查期間發現、現已修正的三項問題：

1. **High — control artifact 無法正常 resume**
   - 原因：`asdict()` 保留 tuple，但 JSON round-trip 變 list，直接比較必定失敗。
   - 實證：純記憶體 round-trip 輸出 `tuple list False`。
   - 最新 diff 已改為 JSON-normalized 比較。

2. **High — permutation 可用極少 exchangeable blocks**
   - 原實作只要求至少一組、兩個可交換 blocks，統計資訊量不足。
   - 最新 diff 已加入 frozen 10-block informative floor，fail-closed。

3. **High — exact-history 宣告可缺少完整日曆證據**
   - 原本部分非 calendar 呼叫仍可聲稱 exact。
   - 最新 diff 已要求 calendar、provenance vector 與正 lookback K 同時存在。

其餘審查結果：

- Block permutation 保留 session block、有效值 pattern 及時間位置對齊；非交換 blocks 排除並回報。
- `entry - 1` 正確回溯 decision row；coarse 120/240m history 會投影至第一個對應 60m raw bucket。
- bidask/kbar/tick metadata 與 GP nested lookback 採 `expression + base - 1` 合成；SMMA recursive history 明確 fail-closed。
- locked gate floors未放寬：bootstrap/permutation `0.10`、DSR `0.50`、WF `0.60`、BH `q=0.10`。
- Campaign controls 在任何 leg 啟動前執行；失敗留下 artifact 並終止。
- 未見 final-holdout access、frozen profile/registry 或既有 run artifact 變更。

Residual nit：permutation 的 production strata 仍主要是 session/時間位置，而非 contract/regime。跨換月 locked window 的 exchangeability 是需在實際 artifact 中監看的假設，但目前不足以構成 confirmed defect。

## Verdict

**APPROVE-WITH-NITS**

## Commands run

- `git diff --check 28f37adb`：clean。
- Focused suite：`158 passed, 1 failed`；唯一失敗為 sandbox 禁止 forkserver Unix socket（`PermissionError: Operation not permitted`），不是 assertion failure。
- JSON round-trip probe：確認舊實作 `tuple list False`。

## Not verified

- 修正後 process-pool test 的非 sandbox 重跑因 approval 等待被中止。
- 未執行 live ClickHouse、完整 campaign、`make ci` 或 final holdout。

## Review verdict

APPROVE-WITH-NITS — no remaining confirmed Tier-3 blocker; production contract/regime permutation strata remains an explicitly monitored assumption.
