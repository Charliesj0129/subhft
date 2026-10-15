@AGENTS.md

# Claude Code notes

Project rules, including the laws (Law 4 / Precision Law), red lines, and the
report template, live in `AGENTS.md` (imported above). It is the single source;
edit it there, not here.

## Subagents (`.claude/agents/`, bound to the roles in AGENTS.md)

| subagent_type | Use for | Model | Tools |
|---|---|---|---|
| hft-executor | one handoff packet, Tier 1/2 | sonnet | edit + bash |
| hft-reviewer | independent review of one named diff | inherit | read-only |
| hft-test-writer | tests under `tests/` | sonnet | edit + bash |
| hft-docs | mechanical docs and path verification | haiku | edit + bash |

## Hooks (`.claude/hooks/`, registered in `.claude/settings.json`)

- `scope_guard` — during a delegation window, writes outside the packet allowlist are denied.
- `git_guard` — subagents' non-read-only git is denied.
- `codex_review_gate` — `git push` / `gh pr create|merge` of `src/`, `rust_core/`, or `config/` changes needs a Codex review attestation.
- `discipline_feedback` — advisory `check_discipline` on the edited platform file.
- `commit_audit` — advisory: HEAD vs the declared allowlist marker.

Hooks and `permissions.ask`/`deny` enforce the red lines; they add no policy.

## Skills and memory

- Skills: `.agent/skills/<name>/SKILL.md`, exposed to Claude Code as
  `.claude/skills` and to Codex as `.agents/skills` (symlinks). The
  frontmatter `description` says when each applies.
- Nested `AGENTS.md` files (`rust_core/`, `tests/`, `research/`) are imported
  by a sibling `CLAUDE.md` and load when you work in that subtree.
- Auto-memory (`MEMORY.md`) is an index with a hard size cap; `.agent/memory/`
  and the repo are the source of truth for laws and risks. Memory notes can be
  stale: verify a named file, flag, or number before acting on it.
