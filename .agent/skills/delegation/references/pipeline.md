# Implement pipeline: packet -> execute -> review -> land -> ledger

Read when a Tier 1/2 code+test task is delegated. The pipeline defines how a
delegation runs, never whether to delegate.

1. **Packet.** Fill a packet (`packet.md`). Write the transient marker
   active-packet.json in the .agent/runtime directory:
   `{"id": "<slug>", "allowed": [<ALLOWED FILES>], "orchestrator_bypass": true}`.
   The `scope_guard` hook then denies writes outside the allowlist.
2. **Execute.** Spawn `hft-executor` (or `hft-test-writer`/`hft-docs`) with the
   packet. Its four-table report is archive section 2.
3. **Review.** Spawn `hft-reviewer` on the named diff (sync; verdict mandatory),
   or review yourself. REQUEST-CHANGES: send a patch packet or fix it directly.
   Two failed attempts on one task: stop and ask the user.
4. **Land.** Re-verify personally (`strict-code-review` Step 0). Mirror the
   allowlist into commit-allowlist.json (same directory), run
   `ALLOWED_PATHS="<files>" bash scripts/check_git_preconditions.sh --narrow-commit`
   (read the exit code directly, not through a pipe), commit on a feature
   branch, then delete BOTH runtime markers. A stale active-packet.json
   blocks unrelated work; clear it first in any new session.
5. **Ledger.** Record the outcome and net-win in `.agent/memory/model-routing.md`
   (schema there) and archive packet + report + verdict verbatim under
   `.agent/memory/delegations/`.

Long tasks that outlive a context window: keep a resumable block in
`.agent/memory/current_session.md` (done units with commit hashes, exact next
step, verification state), updated after each unit and deleted at completion.
