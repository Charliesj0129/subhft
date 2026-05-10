"""Unit tests for ``hft_platform.replay.eligibility`` (loop_v1 L4).

Covers each branch of ``check_eligibility`` and the ``_count_live_intents``
helper directly with a fake CK client, so the L4 gate is exercised by the
unit-test coverage floor (the integration test path is gated out of
``coverage-domain``).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from hft_platform.replay.eligibility import (
    Eligible,
    IneligibleNoFixture,
    IneligiblePreRecorder,
    _count_live_intents,
    check_eligibility,
)


class _FakeCKResult:
    def __init__(self, rows: list[tuple]) -> None:
        self.result_rows = rows


class _FakeCKClient:
    def __init__(self, rows: list[tuple] | Exception) -> None:
        self._rows = rows
        self.calls: list[tuple[str, dict]] = []

    def query(self, q: str, parameters: dict | None = None) -> _FakeCKResult:
        self.calls.append((q, parameters or {}))
        if isinstance(self._rows, Exception):
            raise self._rows
        return _FakeCKResult(self._rows)


def test_count_live_intents_returns_zero_for_empty_rows() -> None:
    client = _FakeCKClient(rows=[])
    n = _count_live_intents(client, date(2026, 4, 21), "ECHO")
    assert n == 0
    # Verify the partition-aligned predicate was used.
    sql, params = client.calls[0]
    assert "ingest_ts" in sql
    assert params == {"d": "2026-04-21", "s": "ECHO"}


def test_count_live_intents_returns_first_column_of_first_row() -> None:
    client = _FakeCKClient(rows=[(7,)])
    assert _count_live_intents(client, date(2026, 5, 5), "R47_MAKER_TMF") == 7


def test_count_live_intents_handles_missing_result_rows_attribute() -> None:
    class _NoAttrResult:
        pass

    class _NoAttrClient:
        def query(self, q: str, parameters: dict | None = None) -> _NoAttrResult:
            return _NoAttrResult()

    assert _count_live_intents(_NoAttrClient(), date(2026, 5, 5), "X") == 0


def test_check_eligibility_no_fixture(tmp_path: Path) -> None:
    missing = tmp_path / "absent.tar.gz"
    result = check_eligibility(
        session_date=date(2026, 4, 21),
        strategy_id="ECHO",
        fixture_path=missing,
        ck_client=_FakeCKClient(rows=[(99,)]),
    )
    assert isinstance(result, IneligibleNoFixture)
    assert result.fixture_path == str(missing)


def test_check_eligibility_pre_recorder_when_count_is_zero(tmp_path: Path) -> None:
    fp = tmp_path / "wal.tar.gz"
    fp.write_bytes(b"placeholder-fixture")
    client = _FakeCKClient(rows=[(0,)])

    result = check_eligibility(
        session_date=date(2026, 4, 21),
        strategy_id="ECHO",
        fixture_path=fp,
        ck_client=client,
    )
    assert isinstance(result, IneligiblePreRecorder)
    assert "no_intents_recorded_for_2026-04-21" in result.reason
    assert "ECHO" in result.reason


def test_check_eligibility_eligible_when_rows_present(tmp_path: Path) -> None:
    fp = tmp_path / "wal.tar.gz"
    fp.write_bytes(b"x")
    client = _FakeCKClient(rows=[(42,)])

    result = check_eligibility(
        session_date=date(2026, 5, 5),
        strategy_id="R47_MAKER_TMF",
        fixture_path=fp,
        ck_client=client,
    )
    assert isinstance(result, Eligible)
    assert result.n_live_intents == 42


def test_check_eligibility_ck_failure_falls_back_to_pre_recorder(tmp_path: Path) -> None:
    fp = tmp_path / "wal.tar.gz"
    fp.write_bytes(b"x")
    client = _FakeCKClient(rows=RuntimeError("ck unreachable"))

    result = check_eligibility(
        session_date=date(2026, 5, 5),
        strategy_id="ECHO",
        fixture_path=fp,
        ck_client=client,
    )
    assert isinstance(result, IneligiblePreRecorder)
    assert "intent_recorder_query_failed" in result.reason
    assert "RuntimeError" in result.reason


# ---------------------------------------------------------------------------
# F2 fix coverage — `_default_ck_client` delegates to the canonical CH factory.
#
# Background mirrors `tests/unit/alpha/test_audit.py`: this module's
# `_default_ck_client` had a comment "Mirrors hft_platform.alpha.audit._get_client
# to keep credential handling consistent" and propagated the same auth-bypass
# bug. After the fix it must delegate to `infra.ch_client.get_ch_client`.
# See `docs/runbooks/alpha-factory-dogfood-2026-05-06.md` §F2.
# ---------------------------------------------------------------------------


def test_default_ck_client_delegates_to_canonical_factory(monkeypatch) -> None:
    """`_default_ck_client` must route through `infra.ch_client.get_ch_client`."""
    from typing import Any

    from hft_platform.replay import eligibility

    sentinel = object()
    calls: list[dict[str, Any]] = []

    def fake_get_ch_client(**kwargs: Any) -> Any:
        calls.append(dict(kwargs))
        return sentinel

    monkeypatch.setattr("hft_platform.infra.ch_client.get_ch_client", fake_get_ch_client)

    result = eligibility._default_ck_client()

    assert result is sentinel
    assert len(calls) == 1, f"expected exactly one call to canonical factory, got {len(calls)}"


# ---------------------------------------------------------------------------
# F2-followup coverage — `_default_ck_client` failures must degrade gracefully.
#
# Codex adversarial-review (2026-05-07) finding [HIGH]: the F2 switch to the
# canonical CH factory introduced a regression where connection / auth /
# import / env-parse failures from ``get_ch_client()`` propagate out of
# ``check_eligibility`` instead of being caught and converted into an
# ``IneligiblePreRecorder`` fallback. The fix wraps the default-client
# construction in its own exception boundary with the
# ``intent_recorder_client_init_failed:`` reason prefix so operators can
# distinguish client-init failures from query failures during triage.
# ---------------------------------------------------------------------------


import pytest


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("ck host unreachable"),
        ImportError("clickhouse_connect not installed"),
        KeyError("HFT_CLICKHOUSE_HOST"),
        RuntimeError("env parse failure"),
    ],
)
def test_check_eligibility_default_client_init_failure_falls_back_to_pre_recorder(
    tmp_path: Path, monkeypatch, exc: Exception
) -> None:
    """When ``_default_ck_client()`` raises during construction, the result
    must be ``IneligiblePreRecorder`` with the ``intent_recorder_client_init_failed:``
    reason prefix — never propagate.
    """
    from hft_platform.replay import eligibility

    fp = tmp_path / "wal.tar.gz"
    fp.write_bytes(b"x")

    def _raise() -> None:
        raise exc

    monkeypatch.setattr(eligibility, "_default_ck_client", _raise)

    result = check_eligibility(
        session_date=date(2026, 5, 7),
        strategy_id="ECHO",
        fixture_path=fp,
        # ck_client=None forces the default-client path
    )
    assert isinstance(result, IneligiblePreRecorder)
    assert "intent_recorder_client_init_failed" in result.reason
    assert type(exc).__name__ in result.reason


def test_default_ck_client_does_not_call_clickhouse_connect_directly(monkeypatch) -> None:
    """Regression guard: must NOT bypass the factory."""
    from typing import Any

    from hft_platform.replay import eligibility

    canonical_calls: list[dict[str, Any]] = []
    direct_calls: list[dict[str, Any]] = []

    def fake_get_ch_client(**kwargs: Any) -> Any:
        canonical_calls.append(dict(kwargs))
        return object()

    monkeypatch.setattr("hft_platform.infra.ch_client.get_ch_client", fake_get_ch_client)

    import clickhouse_connect

    real_get_client = clickhouse_connect.get_client

    def spy_get_client(*args: Any, **kwargs: Any) -> Any:
        direct_calls.append(dict(kwargs))
        return real_get_client(*args, **kwargs)

    monkeypatch.setattr(clickhouse_connect, "get_client", spy_get_client)

    eligibility._default_ck_client()

    assert len(canonical_calls) == 1
    assert direct_calls == [], (
        f"_default_ck_client must not call clickhouse_connect.get_client directly; detected: {direct_calls!r}"
    )
