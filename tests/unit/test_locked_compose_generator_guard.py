"""The locked-compose generator must produce a loadable, in-place artifact.

Two properties of ``docker compose config --no-interpolate`` make the generated
file wrong in ways that are invisible in a diff:

* the Compose project name is derived from the *directory name* and stamped
  onto the default network and every named volume, and bind sources are
  emitted as absolute paths -- so regenerating from a git worktree quietly
  repoints production at empty volumes and different host directories;
* a ``${VAR}``-sourced mount has no interpolated source to classify, so it is
  rendered as long-syntax ``type: volume`` and Compose then rejects it as an
  undefined named volume.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "generate_locked_compose.py"


@pytest.fixture()
def generator(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    spec = importlib.util.spec_from_file_location("generate_locked_compose_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # setitem, not assignment: the module must not outlive the test and leak
    # into a later one running in the same pytest worker.
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "LOCKED_COMPOSE", tmp_path / "docker-compose.prod.locked.yml")
    return module


def _compose(*, name: str = "hft_platform", wal_source: str = "/srv/hft/.wal") -> dict[str, Any]:
    return {
        "name": name,
        "services": {
            "hft-engine": {
                "volumes": [
                    {"type": "bind", "source": wal_source, "target": "/app/.wal"},
                    {"type": "volume", "source": "ch_data_hot", "target": "/var/lib/clickhouse"},
                ]
            }
        },
    }


def _commit(generator: Any, compose: dict[str, Any]) -> None:
    generator.LOCKED_COMPOSE.write_text(yaml.safe_dump(compose), encoding="utf-8")


def _permits(generator: Any, candidate: dict[str, Any]) -> bool:
    """True when the guard lets ``candidate`` be written over the committed file."""
    try:
        generator._assert_matches_committed_identity(candidate)
    except SystemExit:
        return False
    return True


# --------------------------------------------------------------------------
# Identity guard: project name and bind roots
# --------------------------------------------------------------------------


def test_an_unchanged_regeneration_is_permitted(generator: Any) -> None:
    _commit(generator, _compose())
    assert _permits(generator, _compose())


def test_a_changed_project_name_refuses(generator: Any) -> None:
    _commit(generator, _compose(name="hft_platform"))
    with pytest.raises(SystemExit) as excinfo:
        generator._assert_matches_committed_identity(_compose(name="some-worktree"))
    message = str(excinfo.value)
    assert "hft_platform" in message
    assert "some-worktree" in message
    assert "COMPOSE_PROJECT_NAME=hft_platform" in message


def test_a_moved_bind_source_refuses(generator: Any) -> None:
    _commit(generator, _compose(wal_source="/srv/hft/.wal"))
    with pytest.raises(SystemExit) as excinfo:
        generator._assert_matches_committed_identity(_compose(wal_source="/tmp/worktree/.wal"))
    message = str(excinfo.value)
    assert "/srv/hft/.wal" in message
    assert "/tmp/worktree/.wal" in message
    assert "hft-engine:/app/.wal" in message


def test_a_newly_added_bind_mount_is_not_a_move(generator: Any) -> None:
    """Adding a mount is the point of a regeneration; only repointing is not."""
    _commit(generator, _compose())
    candidate = _compose()
    candidate["services"]["hft-engine"]["volumes"].append(
        {"type": "bind", "source": "/srv/hft/outputs", "target": "/app/outputs"}
    )
    assert _permits(generator, candidate)


def test_a_removed_bind_mount_is_not_a_move(generator: Any) -> None:
    _commit(generator, _compose())
    candidate = _compose()
    candidate["services"]["hft-engine"]["volumes"] = [
        v for v in candidate["services"]["hft-engine"]["volumes"] if v.get("type") != "bind"
    ]
    assert _permits(generator, candidate)


def test_a_renamed_named_volume_is_not_read_as_a_bind_move(generator: Any) -> None:
    """Named volumes carry the project prefix; only ``type: bind`` has a host path."""
    _commit(generator, _compose())
    candidate = _compose()
    candidate["services"]["hft-engine"]["volumes"][1]["source"] = "ch_data_cold"
    assert _permits(generator, candidate)


def test_no_committed_file_is_a_first_generation(generator: Any) -> None:
    assert not generator.LOCKED_COMPOSE.exists()
    assert _permits(generator, _compose(name="anything"))


def test_a_committed_file_without_a_project_name_only_checks_binds(generator: Any) -> None:
    """Older locked files predate ``name:``; that must not block a regeneration."""
    committed = _compose()
    del committed["name"]
    _commit(generator, committed)
    assert _permits(generator, _compose(name="hft_platform"))


@pytest.mark.parametrize("body", ["", "[]", "- a\n- b", "just a string"])
def test_a_committed_file_that_is_not_a_mapping_refuses(generator: Any, body: str) -> None:
    """An existing file that lost its shape is damage, not an absent baseline."""
    generator.LOCKED_COMPOSE.write_text(body, encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        generator._assert_matches_committed_identity(_compose(name="some-worktree"))
    assert "not a mapping" in str(excinfo.value)


def test_an_unparseable_committed_file_refuses_rather_than_overwriting(generator: Any) -> None:
    generator.LOCKED_COMPOSE.write_text("{{ not: valid: yaml", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        generator._assert_matches_committed_identity(_compose())
    assert "verify identity" in str(excinfo.value)


# --------------------------------------------------------------------------
# Parameterized mounts must stay in short syntax
# --------------------------------------------------------------------------


def _parameterized(source: str, target: str, *, read_only: bool = False) -> dict[str, Any]:
    entry: dict[str, Any] = {"type": "volume", "source": source, "target": target, "volume": {}}
    if read_only:
        entry["read_only"] = True
    return entry


def test_a_parameterized_mount_is_re_emitted_in_short_syntax(generator: Any) -> None:
    compose = {
        "services": {"clickhouse": {"volumes": [_parameterized("${CH_BACKUP_PATH:-./backups/clickhouse}", "/backups")]}}
    }
    assert generator._restore_parameterized_short_syntax(compose) == 1
    assert compose["services"]["clickhouse"]["volumes"] == ["${CH_BACKUP_PATH:-./backups/clickhouse}:/backups"]


def test_a_read_only_parameterized_mount_keeps_its_ro_flag(generator: Any) -> None:
    compose = {
        "services": {
            "hft-engine": {"volumes": [_parameterized("${CA_CERT_DIR:-./certs}", "/app/certs", read_only=True)]}
        }
    }
    assert generator._restore_parameterized_short_syntax(compose) == 1
    assert compose["services"]["hft-engine"]["volumes"] == ["${CA_CERT_DIR:-./certs}:/app/certs:ro"]


def test_a_parameterized_mount_defaulting_to_a_named_volume_is_also_short_syntax(generator: Any) -> None:
    """The classification is Compose's to make at interpolation time, not ours.

    ``CH_DATA_HOT`` defaults to a named volume but exists so an operator can
    point it at an NVMe path; freezing either answer is wrong for the other.
    """
    compose = {
        "services": {
            "clickhouse": {"volumes": [_parameterized("${CH_DATA_HOT:-ch_data_hot}", "/var/lib/clickhouse/data/hot")]}
        }
    }
    assert generator._restore_parameterized_short_syntax(compose) == 1
    assert compose["services"]["clickhouse"]["volumes"] == ["${CH_DATA_HOT:-ch_data_hot}:/var/lib/clickhouse/data/hot"]


def test_a_literal_mount_is_left_in_long_syntax(generator: Any) -> None:
    compose = {
        "services": {
            "hft-engine": {
                "volumes": [
                    {"type": "bind", "source": "/srv/hft/.wal", "target": "/app/.wal"},
                    {"type": "volume", "source": "ch_metadata", "target": "/var/lib/clickhouse"},
                ]
            }
        }
    }
    before = [dict(v) for v in compose["services"]["hft-engine"]["volumes"]]
    assert generator._restore_parameterized_short_syntax(compose) == 0
    assert compose["services"]["hft-engine"]["volumes"] == before


def test_a_service_with_no_volumes_is_untouched(generator: Any) -> None:
    compose = {"services": {"hft-bot": {"image": "hft:latest"}}}
    assert generator._restore_parameterized_short_syntax(compose) == 0
    assert compose["services"]["hft-bot"] == {"image": "hft:latest"}
