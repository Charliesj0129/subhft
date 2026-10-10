# Prompt principles for this repo

Standard for AGENTS.md, nested AGENTS.md, rules, subagents, and skills.
Derived from OpenAI's guidance for GPT-6 Astra prompts and skills, the Codex
`skill-creator` and `review-agent` samples, and the Claude Code `plugin-dev`
plugin. Check new and revised prompts against it.

1. **Assume capability.** Write only what changes a decision: project facts,
   traps, commands, paths, invariants. Delete generic Python, Rust, testing,
   or git tutorials.
2. **Situational pointers.** "When you do X, read Y", never "before every
   edit read A, B, C". Mandatory pre-reading taxes every task, including a typo fix.
3. **Hard edges, open middle.** Money, production, git history, and secrets get
   fixed steps and red lines. Everything else states the goal and the judgment
   criteria, not a script.
4. **Grant the safe loops.** Say explicitly that running local tests, lint,
   typecheck, and fixing what you broke, then rerunning, needs no asking.
5. **Define done up front**, by blast radius, instead of adding review
   checkpoints mid-task.
6. **One source per rule.** Write a rule once; elsewhere point to it. Keep
   dates, counts, and "as of" numbers out of always-loaded files; they rot.
7. **The description is the index.** Format: `<capability>. Use when
   <specific situations>. Not for <easy confusions>.` One line, at most 300
   characters (hard limit 1024), no `<` or `>` characters, no leading
   comment before the frontmatter. `name` equals the directory name
   (`^[a-z0-9-]{1,64}$`).
8. **Progressive disclosure.** SKILL.md is a router, usually under 800 words:
   invariants, mode selection, project facts that change decisions, Done
   when, and a References list saying when to read each file. Detail lives in
   `references/*.md`. No README inside a skill.
9. **Cross-model.** Shared files avoid model-specific tone and tool-specific
   mechanisms. Claude-only content belongs in `CLAUDE.md`; Codex-only in Codex config.
10. **Verifiable reports.** Findings and results carry file:line, exact
    commands, and literal status words (VERIFIED, WRITTEN, NOT VERIFIED,
    FAILED, BLOCKED). Draw flows and races in ASCII with real identifiers.

## Checks before you ship a prompt

- Every command exists: `make -n <target>`, `uv run hft ... --help`.
- Every path exists: `ls`, `rg --files`; `make agent-docs-check` passes.
- A skill's description would trigger on the task you wrote it for, and not
  on the neighbouring skill's task.
- Safety content survived: if you cut a rule, name where it now lives.
