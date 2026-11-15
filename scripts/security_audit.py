"""Audit the complete public Python dependency lock without installing extras."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def audit_batches(export: str) -> list[list[str]]:
    """Include every pinned version, even those behind another platform's marker."""
    batches: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in export.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        requirement = Requirement(line)
        specifiers = list(requirement.specifier)
        if requirement.url or requirement.extras or len(specifiers) != 1:
            raise ValueError(f"unsupported locked requirement: {line}")
        specifier = specifiers[0]
        if specifier.operator != "==" or "*" in specifier.version:
            raise ValueError(f"requirement is not exactly pinned: {line}")
        name = canonicalize_name(requirement.name)
        key = (name, specifier.version)
        if key in seen:
            continue
        seen.add(key)
        pin = f"{name}=={specifier.version}"
        for batch in batches:
            if name not in batch:
                batch[name] = pin
                break
        else:
            batches.append({name: pin})
    if not seen:
        raise ValueError("locked dependency export contains no requirements")
    return [list(batch.values()) for batch in batches]


def main() -> int:
    if not (ROOT / "uv.lock").is_file():
        print("Security audit failed: repository uv.lock is missing.", flush=True)
        return 1

    with tempfile.TemporaryDirectory(prefix="hft-security-audit-") as directory:
        requirements = Path(directory) / "requirements.txt"
        export = [
            "uv",
            "export",
            "--locked",
            "--format",
            "requirements-txt",
            "--no-hashes",
            "--no-emit-project",
            "--group",
            "dev",
            "--group",
            "research",
            "--extra",
            "bot",
            "--extra",
            "monitor",
            "--output-file",
            str(requirements),
        ]
        try:
            result = subprocess.run(export, cwd=ROOT, capture_output=True, text=True, check=False)
        except OSError as error:
            print(f"Security audit failed: cannot run uv export: {error}", flush=True)
            return 1
        if result.returncode:
            print("Security audit failed: locked dependency export failed.", flush=True)
            print(result.stdout, end="", flush=True)
            print(result.stderr, end="", flush=True)
            return result.returncode
        if not requirements.is_file() or not requirements.read_text().strip():
            print("Security audit failed: locked dependency export is empty or missing.", flush=True)
            return 1

        try:
            batches = audit_batches(requirements.read_text())
        except (InvalidRequirement, ValueError) as error:
            print(f"Security audit failed: invalid locked dependency export: {error}", flush=True)
            return 1

        print("Auditing locked runtime, dev, research, bot and monitor dependencies.", flush=True)
        print("The private Fubon SDK is outside the public lock and is not audited.", flush=True)
        audit = [
            sys.executable,
            "-m",
            "pip_audit",
            "--requirement",
            str(requirements),
            "--strict",
            "--no-deps",
            "--disable-pip",
            "--progress-spinner",
            "off",
        ]
        status = 0
        for index, batch in enumerate(batches, start=1):
            requirements.write_text("\n".join(batch) + "\n")
            print(f"Auditing dependency batch {index}/{len(batches)} ({len(batch)} pins).", flush=True)
            try:
                result = subprocess.run(audit, cwd=ROOT, stderr=subprocess.STDOUT, check=False)
            except OSError as error:
                print(f"Security audit failed: cannot run pip-audit: {error}", flush=True)
                return 1
            if result.returncode:
                status = result.returncode
        return status


if __name__ == "__main__":
    raise SystemExit(main())
