"""The release watcher must not miss a fix release on the pinned line.

`.github/dependabot.yml` ignores `shioaji` outright, so nothing announces a new
SinoPac SDK release any more. These tests pin the behaviour that replaces it:
what counts as newer, what counts as adoptable, and what counts as unassessed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from scripts.shioaji_api_diff import watch


def _payload(*versions: str, yanked: tuple[str, ...] = ()) -> dict[str, Any]:
    """A minimal PyPI ``/pypi/<pkg>/json`` body listing ``versions``."""
    return {"releases": {v: [{"filename": f"shioaji-{v}.whl", "yanked": v in yanked}] for v in versions}}


def _pyproject(tmp_path: Any, requirement: str) -> Any:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        f'[project]\nname = "x"\nversion = "0"\ndependencies = ["msgspec", "{requirement}"]\n',
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# Reading the pin
# --------------------------------------------------------------------------- #
def test_the_pin_is_read_through_the_extras_marker(tmp_path: Any) -> None:
    """``shioaji[speed]==1.5.6`` pins 1.5.6 — the extras are part of the name."""
    assert watch.read_pin(_pyproject(tmp_path, "shioaji[speed]==1.5.6")) == "1.5.6"


def test_a_pin_without_extras_is_read_too(tmp_path: Any) -> None:
    assert watch.read_pin(_pyproject(tmp_path, "shioaji==1.5.6")) == "1.5.6"


def test_a_missing_shioaji_requirement_refuses_rather_than_defaulting(tmp_path: Any) -> None:
    """No pin means every comparison below is meaningless, so it must not guess."""
    with pytest.raises(SystemExit):
        watch.read_pin(_pyproject(tmp_path, "numpy==2.0.0"))


# --------------------------------------------------------------------------- #
# What is newer
# --------------------------------------------------------------------------- #
def test_a_patch_release_on_the_pinned_line_is_reported(tmp_path: Any) -> None:
    """1.5.6 -> 1.5.7 is the case dependabot's blanket ignore silently hides."""
    found = watch.newer_releases("1.5.6", ["1.5.5", "1.5.6", "1.5.7"])
    assert [(r.version, r.gap) for r in found] == [("1.5.7", watch.PATCH)]


def test_the_pinned_version_itself_is_not_an_upgrade_candidate() -> None:
    assert watch.newer_releases("1.5.6", ["1.5.6"]) == []


def test_older_releases_are_not_reported() -> None:
    assert watch.newer_releases("1.5.6", ["1.2.9", "1.3.3", "1.5.3"]) == []


def test_a_new_minor_line_is_reported_as_minor() -> None:
    found = watch.newer_releases("1.5.6", ["1.7.0"])
    assert [r.gap for r in found] == [watch.MINOR]


def test_a_new_major_line_is_reported_as_major() -> None:
    found = watch.newer_releases("1.5.6", ["2.0.0"])
    assert [r.gap for r in found] == [watch.MAJOR]


def test_prerelease_versions_are_not_offered_as_upgrade_candidates() -> None:
    """Shioaji's history carries ``0.3.6.dev4``-style versions; a production pin
    must never be handed one as a candidate."""
    assert watch.newer_releases("1.5.6", ["1.7.0.dev1", "9.9.9.dev0"]) == []


def test_a_release_whose_every_file_is_yanked_is_not_offered() -> None:
    versions = watch.releases_from_payload(_payload("1.5.7", "1.7.0", yanked=("1.7.0",)))
    assert versions == ["1.5.7"]


def test_a_yanked_file_alongside_a_live_one_still_leaves_the_release_installable() -> None:
    payload = {
        "releases": {
            "1.5.7": [
                {"filename": "a.whl", "yanked": True},
                {"filename": "b.whl", "yanked": False},
            ]
        }
    }
    assert watch.releases_from_payload(payload) == ["1.5.7"]


# --------------------------------------------------------------------------- #
# Ordering and assessment state
# --------------------------------------------------------------------------- #
def test_the_patch_on_the_pinned_line_is_listed_before_a_newer_feature_line() -> None:
    """Adoptability, not severity: the patch is the row an operator can act on."""
    found = watch.newer_releases("1.5.6", ["1.7.3", "1.7.0", "1.5.7", "2.0.0"])
    assert [r.version for r in found] == ["1.5.7", "1.7.0", "1.7.3", "2.0.0"]


def test_a_release_with_a_captured_surface_is_not_flagged_unassessed(monkeypatch: Any) -> None:
    monkeypatch.setattr(watch, "_captured_versions", lambda: {"1.5.7"})
    found = watch.newer_releases("1.5.6", ["1.5.7", "1.7.0"])
    assert [(r.version, r.assessed) for r in found] == [("1.5.7", True), ("1.7.0", False)]


def test_the_report_counts_only_uncaptured_releases_as_unassessed(monkeypatch: Any) -> None:
    monkeypatch.setattr(watch, "_captured_versions", lambda: {"1.5.7"})
    report = watch.build_report("1.5.6", watch.newer_releases("1.5.6", ["1.5.7", "1.7.0"]))
    assert report["counts"]["unassessed"] == 1
    # With no ledger passed, the captured release is undecided rather than settled --
    # a surface is evidence, not a decision. See test_release_decisions.py.
    assert report["counts"]["undecided"] == 1
    assert report["counts"]["needs_action"] == 2


def test_the_text_report_names_the_capture_command_for_what_is_missing(
    monkeypatch: Any,
) -> None:
    """A report that only states the gap leaves the operator to rediscover the fix."""
    monkeypatch.setattr(watch, "_captured_versions", set)
    text = watch.render_text(watch.build_report("1.5.6", watch.newer_releases("1.5.6", ["1.5.7"])))
    assert "make shioaji-surface VERSIONS='1.5.7'" in text
    assert "--pair 1.5.6:1.5.7" in text


def test_no_newer_release_reports_that_plainly(monkeypatch: Any) -> None:
    monkeypatch.setattr(watch, "_captured_versions", set)
    text = watch.render_text(watch.build_report("1.5.6", []))
    assert "no newer release on PyPI." in text


# --------------------------------------------------------------------------- #
# The CLI contract a scheduled job depends on
# --------------------------------------------------------------------------- #
def test_strict_mode_exits_nonzero_when_a_release_is_unassessed(tmp_path: Any, monkeypatch: Any, capsys: Any) -> None:
    from scripts.shioaji_api_diff import cli

    releases = tmp_path / "releases.json"
    releases.write_text(json.dumps(_payload("1.5.6", "1.5.7")), encoding="utf-8")
    monkeypatch.setattr(watch, "read_pin", lambda *a, **k: "1.5.6")
    monkeypatch.setattr(watch, "_captured_versions", set)

    rc = cli.main(["watch", "--strict", "--releases-json", str(releases)])

    assert rc == 1
    assert "1.5.7" in capsys.readouterr().out


def test_strict_mode_exits_zero_once_every_release_is_assessed(tmp_path: Any, monkeypatch: Any) -> None:
    from scripts.shioaji_api_diff import cli

    releases = tmp_path / "releases.json"
    releases.write_text(json.dumps(_payload("1.5.6", "1.5.7")), encoding="utf-8")
    monkeypatch.setattr(watch, "read_pin", lambda *a, **k: "1.5.6")
    monkeypatch.setattr(watch, "_captured_versions", lambda: {"1.5.7"})

    assert cli.main(["watch", "--strict", "--releases-json", str(releases)]) == 0


def test_watch_never_calls_the_network_when_given_a_releases_file(tmp_path: Any, monkeypatch: Any) -> None:
    """The offline path must be genuinely offline, or CI failures become flaky."""
    from scripts.shioaji_api_diff import cli

    def _explode(*_a: Any, **_k: Any) -> None:
        raise AssertionError("fetch_releases must not be called with --releases-json")

    releases = tmp_path / "releases.json"
    releases.write_text(json.dumps(_payload("1.5.6")), encoding="utf-8")
    monkeypatch.setattr(watch, "read_pin", lambda *a, **k: "1.5.6")
    monkeypatch.setattr(watch, "fetch_releases", _explode)

    assert cli.main(["watch", "--releases-json", str(releases)]) == 0


# --------------------------------------------------------------------------- #
# The dependabot rule this watcher exists alongside
# --------------------------------------------------------------------------- #
def _shioaji_ignore() -> dict[str, Any]:
    import yaml

    from scripts.shioaji_api_diff.paths import REPO_ROOT

    config = yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    pip = next(u for u in config["updates"] if u["package-ecosystem"] == "pip")
    return next(i for i in pip["ignore"] if i["dependency-name"] == "shioaji")


def test_dependabot_still_announces_a_patch_release_on_the_pinned_shioaji_line() -> None:
    """The blanket `dependency-name: shioaji` ignore hid 1.5.7 for two weeks.

    A patch bump is safe to receive because ``test_sdk_surface_golden`` turns the
    unit job red until someone regenerates the golden — so the PR is a
    notification, not a silent merge. Suppressing it removes the notification and
    keeps none of the safety.
    """
    ignore = _shioaji_ignore()
    types = ignore.get("update-types")
    assert types, "a shioaji ignore with no update-types suppresses patch releases too"
    assert "version-update:semver-patch" not in types


def test_dependabot_still_ignores_shioaji_feature_and_major_lines() -> None:
    """1.5.6 -> 1.7.x is a *minor* bump, so the global major-only rule misses it."""
    types = _shioaji_ignore()["update-types"]
    assert "version-update:semver-minor" in types
    assert "version-update:semver-major" in types
