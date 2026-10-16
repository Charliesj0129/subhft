"""Base images in ``Dockerfile`` and ``docker-compose.yml`` name a patch version (candidate inventory N74).

AGENTS.md asks for "pinned versions". ``redis:7.4`` and ``python:3.12-slim-bookworm`` name only a minor line, so the
bytes behind them change whenever the upstream repository retags, and a rebuild or ``up`` on another host can run a
different Redis or interpreter patch than the one that was tested. These tests read the two real files.

Scope on purpose:

* ``docker-compose.production.yml`` and ``docker-compose.prod.locked.yml`` are Do-NOT-Edit and are not read here.
* The engine image is ``${HFT_IMAGE:-hft-platform:latest}``. It is built from this repository, not pulled, and it is
  the only reference allowed to stay a variable. Making it required in Compose (``${HFT_IMAGE:?}``) is a different
  decision: Compose interpolates the whole file for every command, so ``docker compose ps`` / ``stop`` / ``logs``
  would stop working when it is unset.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE = REPO_ROOT / "docker-compose.yml"

# The one reference that may be a variable; see the module docstring.
ENGINE_IMAGE = "${HFT_IMAGE:-hft-platform:latest}"

# ``7.4.11``, ``v2.51.0``, ``25.12.3``, ``3.12.15-slim-bookworm``: major.minor.patch first, a suffix after it.
PATCH_TAG = re.compile(r"v?\d+\.\d+\.\d+(?:[-.][A-Za-z0-9][A-Za-z0-9._-]*)?")
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")

pytestmark = pytest.mark.unit


def is_pinned(reference: str) -> bool:
    """A digest pins by itself; otherwise the tag must start with a full ``major.minor.patch``."""
    if DIGEST.search(reference):
        return True
    name_and_tag = reference.rsplit("/", 1)[-1]
    if ":" not in name_and_tag:
        return False
    return PATCH_TAG.fullmatch(name_and_tag.split(":", 1)[1]) is not None


def dockerfile_images() -> list[str]:
    """The image of every ``FROM`` line (``--platform`` flags and ``AS`` aliases removed).

    Upper case only: this Dockerfile writes its instructions in upper case, and the Python it embeds in ``RUN``
    heredocs starts lines with a lower-case ``from pathlib import ...``.
    """
    images = []
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        words = line.split()
        if words and words[0] == "FROM":
            images.append(next(word for word in words[1:] if not word.startswith("--")))
    return images


def compose_images() -> list[str]:
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    images = [service["image"] for service in document["services"].values() if "image" in service]
    shared = document.get("x-hft-common", {}).get("image")
    return [*images, *([shared] if shared else [])]


def test_the_scan_finds_what_it_is_meant_to_check():
    """Guards the guard: a parser that finds nothing would pass every other test here."""
    assert len(dockerfile_images()) >= 2
    assert len(compose_images()) >= 8


@pytest.mark.parametrize("reference", dockerfile_images())
def test_dockerfile_base_image_names_a_patch_version(reference):
    assert is_pinned(reference), f"Dockerfile FROM {reference}: name a major.minor.patch tag or a digest"


@pytest.mark.parametrize("reference", sorted(set(compose_images()) - {ENGINE_IMAGE}))
def test_compose_image_names_a_patch_version(reference):
    assert is_pinned(reference), f"docker-compose.yml image {reference}: name a major.minor.patch tag or a digest"


def test_only_the_engine_image_is_a_variable():
    variable = {reference for reference in compose_images() if "${" in reference}

    assert variable <= {ENGINE_IMAGE}, f"unexpected templated image reference(s): {sorted(variable - {ENGINE_IMAGE})}"


@pytest.mark.parametrize(
    "reference",
    [
        "redis:7.4.11",
        "prom/prometheus:v2.51.0",
        "clickhouse/clickhouse-server:25.12.3",
        "grafana/loki:3.0.0",
        "python:3.12.15-slim-bookworm",
        "registry.example:5000/team/app:1.2.3",
        "redis@sha256:" + "a" * 64,
        "redis:7.4@sha256:" + "b" * 64,
    ],
)
def test_pin_check_accepts_patch_tags_and_digests(reference):
    assert is_pinned(reference)


@pytest.mark.parametrize(
    "reference",
    [
        "redis",
        "redis:latest",
        "redis:7",
        "redis:7.4",
        "python:3.12-slim-bookworm",
        "hft-platform:latest",
        "registry.example:5000/team/app",
        "redis:7.4.x",
        "redis:7.4@sha256:short",
    ],
)
def test_pin_check_rejects_floating_references(reference):
    assert not is_pinned(reference)
