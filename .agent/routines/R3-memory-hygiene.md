---
name: R3-memory-hygiene
schedule: "0 9 1 * *"   # 1st of month, 09:00 Taipei
write_scope: none
notify: telegram
venue: routine-worktree
---

# Monthly memory hygiene scan (read-only)

You are a read-only auditor of `.agent/memory/`. List problems; change nothing.

1. Scan `.agent/memory/*.md` for: entries whose dated claims are older than
   90 days and reference in-flight state ("pending", "awaiting", "in flight");
   near-duplicate entries covering the same fact; references to files that no
   longer exist (verify with `rg --files`).
2. Check `.agent/memory/model-routing.md` ledger: any delegation entry after
   2026-07-10 missing its archive file under `.agent/memory/delegations/`.
3. Final message = report: one line per finding (file, line, what, suggested
   disposition: update / merge / delete / keep). Dispositions are suggestions
   for the orchestrator session — you delete and edit nothing.
4. Forbidden: any Edit/Write, any git state change.
