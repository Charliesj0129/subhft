#!/usr/bin/env bash
# Inventory of worktrees, branches and disk hot spots. Deletes nothing.
# Only write: `git fetch origin main` updates the remote-tracking ref.
set -u
R="${1:-$HOME/hft_platform}"
git -C "$R" fetch -q origin main 2>/dev/null || echo "(fetch failed; merged state uses the local origin/main)"
# Branches whose PR is merged on GitHub (catches squash merges that --is-ancestor misses).
MERGED_PR=$(cd "$R" && gh pr list --state merged --limit 400 --json headRefName --jq '.[].headRefName' 2>/dev/null)
printf '%-4s %-58s %-42s %5s %7s %6s %9s %s\n' class path branch dirty merged ahead dataK last
git -C "$R" worktree list --porcelain | awk '
  /^worktree/{p=$2; b="(detached)"; pr=0}
  /^branch/{b=$2}
  /^prunable/{pr=1}
  /^$/{print p, b, pr}
  END{if(p)print p, b, pr}' |
while read -r p b pr; do
  b="${b#refs/heads/}"
  if [ "$pr" = 1 ] || [ ! -d "$p" ]; then
    printf '%-4s %-58s %-42s %s\n' GONE "$p" "$b" "(directory missing; registration only)"
    continue
  fi
  d=$(git -C "$p" status --porcelain | wc -l)
  m=no
  if [ "$b" = "(detached)" ]; then git -C "$R" merge-base --is-ancestor "$(git -C "$p" rev-parse HEAD)" origin/main && m=yes
  elif git -C "$R" merge-base --is-ancestor "$b" origin/main 2>/dev/null; then m=yes
  elif printf '%s\n' "$MERGED_PR" | grep -qxF "$b"; then m=squash; fi
  a=$(git -C "$p" rev-list --count '@{u}..HEAD' 2>/dev/null || echo no-up)
  # Ignored data excluding .venv (hard-linked uv venvs; removing them frees little).
  ig=$(cd "$p" && git ls-files -oi --exclude-standard --directory 2>/dev/null | grep -v '^\.venv/' | tr '\n' '\0' | xargs -0 -r du -sck 2>/dev/null | tail -1 | cut -f1)
  last=$(git -C "$p" log -1 --format=%cs)
  recent=$(( $(date +%s) - $(date -d "$last" +%s) < 3*86400 ))
  if [ "$p" = "$R" ] || [ "$d" -gt 0 ] || [ "$recent" = 1 ]; then c=C
  elif [ "$m" != no ] && { [ "$a" = 0 ] || [ "$a" = no-up ] || [ "$m" = squash ]; } && [ "${ig:-0}" -lt 10240 ]; then c=A
  else c=B; fi
  printf '%-4s %-58s %-42s %5s %7s %6s %9s %s\n' "$c" "$p" "$b" "$d" "$m" "$a" "${ig:-0}" "$last"
  [ "$c" = A ] && echo "     suggest: ! git -C $R worktree remove $p"
done
echo "== suggest for GONE rows: ! git -C $R worktree prune   (metadata only)"
echo "== local branches with no worktree: merged state"
inwt=$(git -C "$R" worktree list --porcelain | awk '/^branch/{sub("refs/heads/","",$2);print $2}')
git -C "$R" for-each-ref --format='%(refname:short)' refs/heads | grep -vxF -f <(printf '%s\n' main $inwt) |
while read -r br; do
  if git -C "$R" merge-base --is-ancestor "$br" origin/main; then s=merged
  elif printf '%s\n' "$MERGED_PR" | grep -qxF "$br"; then s=squash-merged
  else s=unmerged; fi
  echo "$s $br"
done | sort | awk '{n[$1]++} $1!="unmerged"{print "  suggest: ! git -C '"$R"' branch -D " $2 "   # " $1} END{for(k in n) print "  count " k "=" n[k]}'
echo "== disk (WSL view; Windows C:/D: free space is what df shows for /mnt/c, /mnt/d)"
df -h / /mnt/c /mnt/d 2>/dev/null
du -sh "$HOME/hft_worktrees" "$HOME/.claude/projects" "$HOME/.codex/sessions" "$R/research/data" 2>/dev/null
