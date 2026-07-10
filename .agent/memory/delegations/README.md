# Delegation Archive

One file per delegation: `YYYY-MM-DD-<slug>.md`. Written by the orchestrator
when recording the delegation's ledger entry in `model-routing.md`
(institutionalization #5): packets and executor reports used to live only in
dead session transcripts, so ledger entries could not be re-audited against
their sources.

Each archive file contains, verbatim (not paraphrased):

1. **Packet** — the handoff packet exactly as sent to the executor.
2. **Executor final report** — the 4-section report exactly as received
   (or `NONE DELIVERED` + how the outcome was reconstructed).
3. **Review verdict** — one line: verdict + the decisive evidence.

Rules: the matching `model-routing.md` ledger entry links here; no secrets or
credentials ever (packets must already be clean); files are append-only
historical records — never rewritten, and their path claims are dated
snapshots deliberately not checked by `make agent-docs-check`. Ledger entries
dated before 2026-07-10 predate this archive and have no file; never
back-fill from memory.
