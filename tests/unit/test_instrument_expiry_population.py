"""Every row in ``hft.market_data`` carries ``expiry = 1970-01-01``.

Measured on THESHOW 2026-08-12: TMFH6, TMFI6, TXFH6 and TXFI6 all record the
schema default from ``20260330_001_add_instrument_columns.sql:13``. Two
independent causes, and the second is the interesting one.

**Futures never had a chance.** ``normalizer.py:424`` gates the extraction of
strike, right *and* expiry behind ``if itype == InstrumentType.OPTION``, and
``config/symbols.yaml`` carries no ``expiry`` field on any of its 7 futures
entries. So ``InstrumentProfile.expiry`` is ``None`` and
``recorder/mapper.py:42`` falls through to the epoch.

**Options had the data and dropped it silently.** All 106 option entries in
``config/symbols.yaml`` do carry an expiry — as ``"2026/06/17"``.
``date.fromisoformat`` rejects slash separators, and ``normalizer.py:442``
catches the ``ValueError`` with a bare ``pass``. The value was present, parsed,
rejected and discarded without a log line.

That silence has a second consequence away from the audit column.
``InstrumentRegistry.evict_expired`` (``instrument_registry.py:192``) filters
on ``prof.expiry is not None and prof.expiry < as_of``, and it is the *only*
way ``_try_evict_for_space`` (``instrument_registry.py:267``) can free a slot.
With every expiry ``None``, a full registry raises ``InstrumentLimitError``
instead of evicting contracts that expired months ago.

The third-Wednesday derivation used for futures below is checked against the
broker-supplied option data rather than asserted: ``config/symbols.yaml``
gives June 2026 as ``2026/06/17``, and the third Wednesday of June 2026 is the
17th.
"""

from __future__ import annotations

from datetime import date

import pytest

from hft_platform.core.instrument_registry import InstrumentType


def _meta(entries: dict[str, dict]):
    """A SymbolMetadata loaded from a throwaway symbols.yaml.

    Goes through the real load + registry population path rather than poking at
    internals: the defect being pinned lives in that path.
    """
    import tempfile

    import yaml

    from hft_platform.feed_adapter.normalizer import SymbolMetadata

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump({"symbols": list(entries.values())}, fh, allow_unicode=True)
        path = fh.name
    return SymbolMetadata(config_path=path)


_TMFH6 = {
    "code": "TMFH6",
    "name": "小型臺指期貨08",
    "exchange": "FUT",
    "product_type": "future",
    "point_value": 10,
    "tick_size": 1,
    "price_scale": 10000,
}
_TXO_JUNE = {
    "code": "TXO30400R6",
    "name": "臺指選擇權F506月30400P",
    "exchange": "OPT",
    "product_type": "option",
    "right": "P",
    "strike": 30400.0,
    "expiry": "2026/06/17",
    "underlying": "TX",
    "point_value": 50,
    "price_scale": 10000,
}


# --------------------------------------------------------------------------- #
# Options: the data was there all along                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_an_option_expiry_in_the_shipped_slash_format_is_parsed() -> None:
    """``config/symbols.yaml`` writes "2026/06/17"; fromisoformat rejects it and
    the ValueError was swallowed by a bare pass."""
    profile = _meta({"TXO30400R6": _TXO_JUNE}).registry.get("TXO30400R6")

    assert profile.expiry == date(2026, 6, 17)


@pytest.mark.unit
def test_an_iso_option_expiry_still_parses() -> None:
    entry = dict(_TXO_JUNE, expiry="2026-06-17")

    assert _meta({"TXO30400R6": entry}).registry.get("TXO30400R6").expiry == date(2026, 6, 17)


@pytest.mark.unit
def test_an_unparseable_expiry_is_logged_rather_than_silently_dropped() -> None:
    """The bug hid for months because the discard left no trace. An expiry that
    cannot be parsed must leave a record, and must not take the profile with
    it."""
    from structlog.testing import capture_logs

    entry = dict(_TXO_JUNE, expiry="not-a-date")
    with capture_logs() as logs:
        profile = _meta({"TXO30400R6": entry}).registry.get("TXO30400R6")

    assert profile.expiry is None
    assert any(entry_.get("event") == "instrument_expiry_unparseable" for entry_ in logs)


@pytest.mark.unit
def test_an_option_without_an_expiry_field_keeps_a_none_expiry() -> None:
    entry = {k: v for k, v in _TXO_JUNE.items() if k != "expiry"}

    assert _meta({"TXO30400R6": entry}).registry.get("TXO30400R6").expiry is None


# --------------------------------------------------------------------------- #
# Futures: derived from the contract month code                                #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_a_futures_expiry_is_derived_from_its_month_code() -> None:
    """H = the 8th month letter → August 2026. TAIFEX monthly contracts settle
    on the third Wednesday, which in August 2026 is the 19th."""
    profile = _meta({"TMFH6": _TMFH6}).registry.get("TMFH6")

    assert profile.instrument_type is InstrumentType.FUTURE
    assert profile.expiry == date(2026, 8, 19)


@pytest.mark.unit
def test_the_third_wednesday_rule_matches_the_brokers_own_option_data() -> None:
    """The derivation is not asserted from memory. ``config/symbols.yaml``
    ships broker-supplied option expiries; June 2026 is given as 2026/06/17,
    and the rule must reproduce it."""
    from hft_platform.feed_adapter.normalizer import _third_wednesday

    assert _third_wednesday(2026, 6) == date(2026, 6, 17)
    assert _third_wednesday(2026, 8) == date(2026, 8, 19)
    # A month starting on a Wednesday: the third Wednesday is the 15th.
    assert _third_wednesday(2026, 4) == date(2026, 4, 15)


@pytest.mark.unit
def test_an_explicit_futures_expiry_beats_the_derivation() -> None:
    """If a source ever does supply one, the supplied value wins — a derivation
    is a fallback, not an override."""
    entry = dict(_TMFH6, expiry="2026/08/20")

    assert _meta({"TMFH6": entry}).registry.get("TMFH6").expiry == date(2026, 8, 20)


@pytest.mark.unit
def test_a_futures_code_that_is_not_a_monthly_contract_gets_no_derived_expiry() -> None:
    """The third-Wednesday rule holds for monthly TAIFEX contracts. Anything
    that does not match the ``ROOT`` + month-letter + year-digit shape must not
    be given a guessed date."""
    entry = dict(_TMFH6, code="TMFR1")

    assert _meta({"TMFR1": entry}).registry.get("TMFR1").expiry is None


@pytest.mark.unit
def test_an_equity_gets_no_expiry() -> None:
    entry = {"code": "2330", "exchange": "TSE", "product_type": "stock", "price_scale": 10000, "tick_size": 1}

    assert _meta({"2330": entry}).registry.get("2330").expiry is None


# --------------------------------------------------------------------------- #
# What the recorder writes                                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_recorder_writes_a_real_futures_expiry_not_the_epoch() -> None:
    """The observable symptom: every futures row carried the schema default."""
    from hft_platform.recorder.mapper import _instrument_fields

    fields = _instrument_fields("TMFH6", _meta({"TMFH6": _TMFH6}))

    assert fields["expiry"] == date(2026, 8, 19)
    assert fields["instrument_type"] == "future"


@pytest.mark.unit
def test_the_recorder_writes_a_real_option_expiry_not_the_epoch() -> None:
    from hft_platform.recorder.mapper import _instrument_fields

    fields = _instrument_fields("TXO30400R6", _meta({"TXO30400R6": _TXO_JUNE}))

    assert fields["expiry"] == date(2026, 6, 17)


@pytest.mark.unit
def test_an_unknown_symbol_still_falls_back_to_the_epoch() -> None:
    """The fallback is what makes the column non-nullable-safe; it must stay."""
    from hft_platform.recorder.mapper import _instrument_fields

    assert _instrument_fields("NOPE", _meta({}))["expiry"] == date(1970, 1, 1)


# --------------------------------------------------------------------------- #
# The capacity valve that could never open                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_expired_contracts_can_now_be_evicted_to_free_registry_space() -> None:
    """``_try_evict_for_space`` (instrument_registry.py:267) is the only relief
    valve when the registry hits capacity, and it filters on ``expiry``. With
    every expiry None it could never free a single slot, so a full registry
    raised InstrumentLimitError while holding contracts that expired months
    earlier."""
    registry = _meta({"TXO30400R6": _TXO_JUNE, "TMFH6": _TMFH6}).registry

    evicted = registry.evict_expired(as_of=date(2026, 7, 1))

    assert evicted == 1, "the June option is expired as of July; the August future is not"
    assert registry.get("TMFH6") is not None
