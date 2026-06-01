# Project Structure

- `src/`: Python platform code.
- `rust_core/`: PyO3/Rust hot-path kernels.
- `config/`: YAML/JSON runtime and research config.
- `.agent/`: rules, skills, memory, workflows, evals.

Session start: read mandatory indexes in `AGENTS.md`; inspect `.agent/memory/module_gotchas.md` when relevant. Session end or "save/wrap up": update `.agent/memory/current_session.md`; append reusable bugs/perf/arch/gotcha lessons to `.agent/memory/lessons_learned.md`.

Before commit/PR: inspect `git status --short`, stage narrowly, run verification matching blast radius.
