#!/usr/bin/env bash
# Read-only: print the files that hold progress state.
set -u
R="$(git rev-parse --show-toplevel 2>/dev/null || echo "$HOME/hft_platform")"
echo "== newest plans"
ls -t "$HOME"/.claude/plans/*.md 2>/dev/null | head -5 | while read -r f; do
  printf '%s  %s\n' "$(date -r "$f" +%F_%R)" "$f"; done
ls -t "$HOME"/.codex/attachments/*/approved-plan.md 2>/dev/null | head -3
echo "== current_session.md (modified $(date -r "$R/.agent/memory/current_session.md" +%F 2>/dev/null), head)"
sed -n '1,25p' "$R/.agent/memory/current_session.md" 2>/dev/null
echo "== PR draft queue (modified $(date -r "$HOME/hft_worktrees/_pr-drafts/QUEUE.md" +%F 2>/dev/null), head)"
sed -n '1,20p' "$HOME/hft_worktrees/_pr-drafts/QUEUE.md" 2>/dev/null
echo "== newest program memory notes"
ls -t "$HOME"/.claude/projects/-home-charlie-hft-platform/memory/*.md 2>/dev/null | grep -v MEMORY.md | head -5
echo "== open PRs"
gh pr list --state open --limit 40 --json number,title,headRefName,isDraft \
  --template '{{range .}}#{{.number}} {{if .isDraft}}[draft] {{end}}{{.headRefName}} | {{.title}}{{"\n"}}{{end}}' 2>&1 | head -40
echo "== worktrees (path branch last-commit)"
git -C "$R" worktree list --porcelain | awk '/^worktree/{p=$2}/^branch/{print p, $2}' |
while read -r p b; do
  printf '%s %s %s\n' "$p" "${b#refs/heads/}" "$(git -C "$p" log -1 --format='%cs %s' 2>/dev/null | cut -c1-70)"
done
