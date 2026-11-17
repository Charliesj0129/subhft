#!/usr/bin/env bash
# Read-only inventory of worktrees, branches and disk hot spots.
set -u
R="${1:-$HOME/hft_platform}"
git -C "$R" fetch -q origin main 2>/dev/null || echo "(fetch failed; merged state uses the local origin/main)"
printf '%-58s %-42s %5s %6s %6s %9s %s\n' path branch dirty merged ahead ignoredK last
git -C "$R" worktree list --porcelain | awk '/^worktree/{p=$2}/^branch/{print p, $2}/^detached/{print p, "(detached)"}' |
while read -r p b; do
  b="${b#refs/heads/}"
  d=$(git -C "$p" status --porcelain 2>/dev/null | wc -l)
  m=no; git -C "$R" merge-base --is-ancestor "$b" origin/main 2>/dev/null && m=yes
  a=$(git -C "$p" rev-list --count '@{u}..HEAD' 2>/dev/null || echo no-up)
  ig=$(cd "$p" 2>/dev/null && git ls-files -oi --exclude-standard --directory 2>/dev/null | tr '\n' '\0' | xargs -0 -r du -sck 2>/dev/null | tail -1 | cut -f1)
  printf '%-58s %-42s %5s %6s %6s %9s %s\n' "$p" "$b" "$d" "$m" "$a" "${ig:-0}" "$(git -C "$p" log -1 --format=%cs 2>/dev/null)"
done
echo "== local branches merged into origin/main"
git -C "$R" branch --merged origin/main --format='%(refname:short)' | grep -vx main | head -40
echo "== disk"
df -h / /mnt/c /mnt/d 2>/dev/null
du -sh "$HOME/hft_worktrees" "$HOME/.claude/projects" "$HOME/.codex/sessions" "$R/research/data" 2>/dev/null
