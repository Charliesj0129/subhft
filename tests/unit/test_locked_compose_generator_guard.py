"""The locked-compose generator must produce a loadable, in-place artifact.

Three properties of ``docker compose config`` make the generated file wrong in
ways that are invisible in a diff:

* the Compose project name is derived from the *directory name* and stamped
  onto the default network and every named volume, so regenerating from a git
  worktree quietly repoints production at empty volumes;
* every host path is emitted absolute, so the artifact records the checkout
  that produced it -- and a ``required`` ``env_file`` that does not exist on
  the deploy host is a hard load failure, not a warning;
* under ``--no-interpolate`` a ``${VAR}``-sourced mount has no interpolated
  source to classify, so it is rendered as long-syntax ``type: volume`` and
  Compose then rejects it as an undefined named volume.
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


def test_no_committed_file_is_a_first_generation(generator: Any) -> None:
    assert not generator.LOCKED_COMPOSE.exists()
    assert _permits(generator, _compose(name="anything"))


def test_a_committed_file_without_a_project_name_is_permitted(generator: Any) -> None:
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


# --------------------------------------------------------------------------
# Relativization: the artifact must not name the checkout that generated it
# --------------------------------------------------------------------------


@pytest.fixture()
def rooted(generator: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """``generator`` with its repo root pointed at a scratch checkout."""
    monkeypatch.setattr(generator, "REPO_ROOT", tmp_path / "checkout")
    return generator


def _service(rooted: Any, **body: Any) -> dict[str, Any]:
    return {"services": {"hft-engine": body}}


def test_a_bind_source_inside_the_checkout_becomes_relative(rooted: Any) -> None:
    compose = _service(rooted, volumes=[{"type": "bind", "source": f"{rooted.REPO_ROOT}/.wal", "target": "/app/.wal"}])
    assert rooted._relativize_repo_paths(compose) == 1
    assert compose["services"]["hft-engine"]["volumes"][0]["source"] == "./.wal"


def test_an_env_file_inside_the_checkout_becomes_relative(rooted: Any) -> None:
    compose = _service(rooted, env_file=[{"path": f"{rooted.REPO_ROOT}/.env", "required": True}])
    assert rooted._relativize_repo_paths(compose) == 1
    assert compose["services"]["hft-engine"]["env_file"][0]["path"] == "./.env"
    # The requirement must survive: a missing .env has to stay a hard failure.
    assert compose["services"]["hft-engine"]["env_file"][0]["required"] is True


def test_a_string_env_file_inside_the_checkout_becomes_relative(rooted: Any) -> None:
    compose = _service(rooted, env_file=f"{rooted.REPO_ROOT}/.env")
    assert rooted._relativize_repo_paths(compose) == 1
    assert compose["services"]["hft-engine"]["env_file"] == "./.env"


def test_a_build_context_inside_the_checkout_becomes_relative(rooted: Any) -> None:
    """``build.context`` is neither a volume nor an env file, and it leaked."""
    compose = _service(rooted, build={"context": str(rooted.REPO_ROOT), "dockerfile": "Dockerfile"})
    assert rooted._relativize_repo_paths(compose) == 1
    assert compose["services"]["hft-engine"]["build"]["context"] == "."


def test_a_host_path_outside_the_checkout_stays_absolute(rooted: Any) -> None:
    """``/proc`` and the docker socket are real host locations, not ours."""
    compose = _service(
        rooted,
        volumes=[
            {"type": "bind", "source": "/proc", "target": "/host/proc"},
            {"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"},
        ],
    )
    assert rooted._relativize_repo_paths(compose) == 0
    assert [v["source"] for v in compose["services"]["hft-engine"]["volumes"]] == ["/proc", "/var/run/docker.sock"]


def test_a_parameterized_short_syntax_mount_is_left_alone(rooted: Any) -> None:
    compose = _service(rooted, volumes=["${CH_DATA_HOT:-ch_data_hot}:/var/lib/clickhouse"])
    assert rooted._relativize_repo_paths(compose) == 0
    assert compose["services"]["hft-engine"]["volumes"] == ["${CH_DATA_HOT:-ch_data_hot}:/var/lib/clickhouse"]


def test_a_named_volume_source_is_left_alone(rooted: Any) -> None:
    compose = _service(rooted, volumes=[{"type": "volume", "source": "ch_metadata", "target": "/var/lib/clickhouse"}])
    assert rooted._relativize_repo_paths(compose) == 0
    assert compose["services"]["hft-engine"]["volumes"][0]["source"] == "ch_metadata"


def test_a_relativized_compose_passes_the_leak_check(rooted: Any) -> None:
    compose = _service(rooted, volumes=[{"type": "bind", "source": f"{rooted.REPO_ROOT}/data", "target": "/app/data"}])
    rooted._relativize_repo_paths(compose)
    assert rooted._generator_local_paths(compose) == []
    rooted._assert_no_generator_local_paths(compose)  # therefore must not raise


def test_a_checkout_path_under_a_field_the_rewrite_does_not_know_refuses(rooted: Any) -> None:
    """The leak check walks blind so that a new path-valued key fails loudly.

    This is how ``build.context`` was caught: the rewrite has to enumerate its
    fields, so the check that the rewrite was complete must not.
    """
    compose = _service(rooted, some_future_key={"path": f"{rooted.REPO_ROOT}/secrets"})
    assert rooted._relativize_repo_paths(compose) == 0
    with pytest.raises(SystemExit) as excinfo:
        rooted._assert_no_generator_local_paths(compose)
    assert "some_future_key" in str(excinfo.value)


def test_a_compose_with_no_checkout_paths_is_permitted(rooted: Any) -> None:
    compose = _service(rooted, volumes=[{"type": "bind", "source": "./data", "target": "/app/data"}])
    assert rooted._generator_local_paths(compose) == []
    rooted._assert_no_generator_local_paths(compose)  # therefore must not raise
