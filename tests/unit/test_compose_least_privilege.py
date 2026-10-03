"""The Telegram-facing and monitoring containers must not hold what they do not use.

`env_file: .env` in the shared anchor hands every service the whole `.env`, so the broker
identity reached `hft-bot` and `hft-monitor`, neither of which talks to the broker. These
tests pin the narrowing (environment overrides win over env_file) and make sure the one
service that does need the broker, the engine, was not narrowed by accident.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"
_BROKER_VARS = (
    "SHIOAJI_API_KEY",
    "SHIOAJI_SECRET_KEY",
    "SHIOAJI_PERSON_ID",
    "SHIOAJI_ACCOUNT",
    "CA_PASSWORD",
    "CA_CERT_PATH",
)
_NARROWED = ("hft-bot", "hft-monitor")


@pytest.fixture(scope="module")
def services() -> dict[str, Any]:
    return yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))["services"]


def _env(service: dict[str, Any]) -> dict[str, str | None]:
    """Compose list semantics: a later `K=V` wins; a bare `K` passes through (None)."""
    out: dict[str, str | None] = {}
    for item in service.get("environment", []):
        key, sep, value = str(item).partition("=")
        out[key] = value if sep else None
    return out


@pytest.mark.parametrize("name", _NARROWED)
@pytest.mark.parametrize("var", _BROKER_VARS)
def test_broker_identity_is_blanked_for_service_that_never_uses_it(
    services: dict[str, Any], name: str, var: str
) -> None:
    env = _env(services[name])
    assert var in env, f"{name} must override {var}; env_file would otherwise hand it the real value"
    assert env[var] == "", f"{name}: {var} must be overridden to the empty string, got {env[var]!r}"


@pytest.mark.parametrize("var", ("SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY", "SHIOAJI_PERSON_ID"))
def test_engine_still_receives_the_broker_identity(services: dict[str, Any], var: str) -> None:
    value = _env(services["hft-engine"]).get(var)
    assert value and value.startswith("${"), f"hft-engine must keep {var} interpolated from .env, got {value!r}"


@pytest.mark.parametrize("name", _NARROWED)
def test_code_directories_are_mounted_read_only(services: dict[str, Any], name: str) -> None:
    mounts = {str(v).split(":")[1]: str(v).split(":")[2:] for v in services[name]["volumes"]}
    for target in ("/app/src", "/app/config", "/app/scripts"):
        assert mounts.get(target) == ["ro"], f"{name}: {target} must be read-only, got {mounts.get(target)}"


@pytest.mark.parametrize("name", _NARROWED)
def test_all_capabilities_are_dropped(services: dict[str, Any], name: str) -> None:
    assert services[name]["cap_drop"] == ["ALL"]


def test_bot_cannot_gain_privileges(services: dict[str, Any]) -> None:
    assert "no-new-privileges:true" in services["hft-bot"]["security_opt"]


def test_monitor_leaves_no_new_privileges_to_the_production_overlay(services: dict[str, Any]) -> None:
    """compose concatenates `security_opt` across -f files and rejects a duplicate item, so
    setting it here as well as in docker-compose.production.yml breaks the merged render."""
    assert "security_opt" not in services["hft-monitor"]
    overlay = yaml.safe_load((_COMPOSE.parent / "docker-compose.production.yml").read_text(encoding="utf-8"))
    assert "no-new-privileges:true" in overlay["services"]["hft-monitor"]["security_opt"]


@pytest.mark.parametrize("name", _NARROWED)
def test_crash_dump_sink_is_kept(services: dict[str, Any], name: str) -> None:
    targets = [str(v).split(":")[1] for v in services[name]["volumes"]]
    assert "/var/cores" in targets, "core dumps must land on the host, not in the container layer"


def test_engine_mounts_are_untouched(services: dict[str, Any]) -> None:
    """This change must not narrow the engine: its mounts are the deploy surface."""
    targets = {str(v).split(":")[1]: str(v).split(":")[2:] for v in services["hft-engine"]["volumes"]}
    for target in ("/app/src", "/app/config", "/app/scripts"):
        assert targets[target] == [], f"hft-engine {target} must stay read-write, got {targets[target]}"
