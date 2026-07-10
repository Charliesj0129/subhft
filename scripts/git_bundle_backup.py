"""Local git-bundle backup (fail-closed).

Why: pushing is a per-operation human approval, so new local commits sit on a
single disk between approved pushes. A dated `git bundle` written to a second
location outside the repository covers that window without touching any
remote. Institutionalization point #11
(docs/superpowers/specs/2026-07-10-agent-system-institutionalization-design.md).

Fail-closed rules (refuse rather than guess):
  - the destination is REQUIRED and must already exist — never silently created;
  - the destination must be a directory OUTSIDE the repository;
  - an existing bundle file is never overwritten;
  - the bundle is verified (`git bundle verify`) and must cover HEAD,
    otherwise the run fails.

A bundle contains full repository history. Choose destinations accordingly:
a disk Charlie controls, never a shared/synced/public location. The first
real run requires Charlie's one-time destination approval — record each run
(date + destination + bundle name) in `.agent/memory/current-risks.md`.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


class GitError(RuntimeError):
    """A git subcommand failed; message carries the command and stderr."""


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _repo_root(repo: Path) -> Path:
    return Path(_git(["rev-parse", "--show-toplevel"], cwd=repo).strip()).resolve()


def create_backup(repo: Path, dest: Path, stamp: str) -> Path:
    """Create and verify a bundle of ALL refs; return the bundle path.

    Raises GitError on any git failure, ValueError on a fail-closed refusal.
    """
    root = _repo_root(repo)

    if not dest.is_dir():
        raise ValueError(f"destination is not an existing directory (never created silently): {dest}")
    dest = dest.resolve()
    if dest == root or root in dest.parents:
        raise ValueError(f"destination must be OUTSIDE the repository ({root}): {dest}")

    head = _git(["rev-parse", "HEAD"], cwd=root).strip()
    bundle = dest / f"{root.name}-{stamp}-{head[:12]}.bundle"
    if bundle.exists():
        raise ValueError(f"bundle already exists, refusing to overwrite: {bundle}")

    _git(["bundle", "create", str(bundle), "--all"], cwd=root)
    _git(["bundle", "verify", str(bundle)], cwd=root)

    listed = _git(["bundle", "list-heads", str(bundle)], cwd=root)
    if head not in listed:
        bundle.unlink()
        raise GitError(f"bundle does not cover HEAD {head[:12]}; deleted incomplete bundle {bundle}")
    return bundle


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--dest",
        required=True,
        type=Path,
        help="existing directory OUTSIDE the repo to write the bundle into (required; never created)",
    )
    parser.add_argument("--repo", type=Path, default=Path("."), help="repository to back up (default: cwd)")
    parser.add_argument(
        "--stamp",
        default=None,
        help="timestamp label for the bundle name (default: current UTC, YYYYMMDDTHHMMSSZ)",
    )
    args = parser.parse_args(argv)
    stamp = args.stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    try:
        bundle = create_backup(args.repo, args.dest, stamp)
    except ValueError as exc:
        print(f"REFUSED (fail-closed): {exc}")
        return 2
    except (GitError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}")
        return 1

    print(f"OK: created and verified {bundle} (all refs, covers HEAD)")
    print("Record this run (date + destination + bundle name) in .agent/memory/current-risks.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
