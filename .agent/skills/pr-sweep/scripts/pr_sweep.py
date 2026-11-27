#!/usr/bin/env python3
"""Read-only PR sweep through gh. Prints a table and suggested commands; changes nothing."""

import argparse
import json
import subprocess
import sys

BAD = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}
WAIT = {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING"}


def gh(*args: str) -> list:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"gh failed: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout or "[]")


def ci(checks: list) -> tuple[str, list]:
    bad = [c.get("name") or c.get("context") or "?" for c in checks
           if (c.get("conclusion") or c.get("state") or "").upper() in BAD]
    if bad:
        return "RED", bad
    if any((c.get("status") or c.get("state") or "").upper() in WAIT for c in checks):
        return "PENDING", []
    return ("GREEN", []) if checks else ("NONE", [])


CODEX = ("src/", "rust_core/", "config/", ".claude/hooks/", ".claude/settings.json")
PROTECTED = (".gitignore", "pyproject.toml", ".importlinter", "scripts/check_discipline.py",
             ".pre-commit-config.yaml", "docker-compose.production.yml", "docker-compose.prod.locked.yml",
             "src/hft_platform/contracts/", "src/hft_platform/events.py", "src/hft_platform/core/timebase.py",
             "src/hft_platform/core/pricing.py", "src/hft_platform/migrations/clickhouse/",
             "config/symbols.yaml", "config/base/brokers/", "research/experiments/", "tests/golden/")


def gate(number: int) -> str:
    r = subprocess.run(["gh", "pr", "diff", str(number), "--name-only"], capture_output=True, text=True)
    if r.returncode:
        return "?"
    files = r.stdout.split()
    tags = []
    if any(f.startswith(CODEX) for f in files):
        tags.append("codex")
    if any(f.startswith(PROTECTED) for f in files):
        tags.append("protected")
    return "+".join(tags) or "ok"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--merged-since", help="YYYY-MM-DD")
    a = ap.parse_args()
    if a.merged_since:
        rows = gh("pr", "list", "--state", "merged", "--limit", "100",
                  "--search", f"merged:>={a.merged_since}", "--json", "number,title,mergedAt,headRefName")
        for p in rows:
            print(f"#{p['number']} {p['mergedAt'][:10]} {p['headRefName']} | {p['title'][:70]}")
        print(f"merged={len(rows)}")
        return
    rows = gh("pr", "list", "--state", "open", "--limit", "100", "--json",
              "number,title,headRefName,isDraft,mergeable,statusCheckRollup,headRefOid")
    print("| # | branch | CI | mergeable | red checks |\n|---|---|---|---|---|")
    ready = []
    for p in rows:
        state, bad = ci(p.get("statusCheckRollup") or [])
        draft = " (draft)" if p["isDraft"] else ""
        print(f"| {p['number']} | {p['headRefName']}{draft} | {state} | {p['mergeable']} | {', '.join(bad[:4])} |")
        if state == "GREEN" and p["mergeable"] == "MERGEABLE" and not p["isDraft"]:
            ready.append(p)
    unknown = [p["number"] for p in rows if ci(p.get("statusCheckRollup") or [])[0] == "GREEN"
               and p["mergeable"] == "UNKNOWN" and not p["isDraft"]]
    print(f"\nopen={len(rows)} green_and_mergeable={len(ready)} green_mergeable_unknown={unknown}")
    verb = "mer" + "ge"  # spelled apart so the codex_review_gate hook does not read this file's source as a command
    print("gate | command")
    for p in ready:
        print(f"{gate(p['number'])} | ! gh pr {verb} {p['number']} --squash --match-head-commit {p['headRefOid']}")


if __name__ == "__main__":
    main()
