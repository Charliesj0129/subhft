"""Track membership must survive a contract roll.

On 2026-08-10 the day session opened correctly (``futures_day`` transitioned
CLOSED -> OPEN at 08:45:00 CST) and R47 called ``buy()``/``sell()`` 2,703
times, but ``alpha_signal_events_total{outcome="intent"}`` stayed at zero:
``config/base/session_governor.yaml`` listed ``TMFE6``/``TXFE6`` — May-2026
contracts — plus R1 aliases that never resolve in this deployment, so the live
front month ``TMFH6`` belonged to no track. ``TrackGate.get_phase`` returned
CLOSED (fail-closed, correct in itself) and ``StrategyRunner`` discarded every
intent, recording each one as ``flat``.

These tests pin the fix: membership by product root, and a live-config guard
that fails if anyone reintroduces a month code.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hft_platform.ops.session_governor import SessionGovernor, SessionPhase, TrackGate

_LIVE_CONFIG = Path(__file__).resolve().parents[2] / "config" / "base" / "session_governor.yaml"

# TAIFEX futures delivery-month codes: A (Jan) .. L (Dec), followed by a single
# year digit — TMFH6 is TMF + March + 2026.
_MONTH_LETTERS = "ABCDEFGHIJKL"


@pytest.mark.unit
class TestRootMembership:
    def test_front_month_resolves_through_its_root_after_a_roll(self) -> None:
        """The exact 2026-08-10 failure: TMFH6 is live, only the root is configured."""
        gate = TrackGate()
        gate.register_root("TMF", "futures_day")
        gate.set_track_phase("futures_day", SessionPhase.OPEN)

        assert gate.get_phase("TMFH6") == SessionPhase.OPEN

    def test_every_month_code_of_a_registered_root_resolves(self) -> None:
        """No future roll may need a config edit."""
        gate = TrackGate()
        gate.register_root("TMF", "futures_day")
        gate.set_track_phase("futures_day", SessionPhase.OPEN)

        for letter in _MONTH_LETTERS:
            for year in "6789":
                assert gate.get_phase(f"TMF{letter}{year}") == SessionPhase.OPEN

    def test_continuous_alias_also_resolves_through_the_root(self) -> None:
        gate = TrackGate()
        gate.register_root("TMF", "futures_day")
        gate.set_track_phase("futures_day", SessionPhase.OPEN)

        assert gate.get_phase("TMFR1") == SessionPhase.OPEN

    def test_unregistered_root_is_still_blocked(self) -> None:
        """Root matching must not become a blanket fail-open."""
        gate = TrackGate()
        gate.register_root("TMF", "futures_day")
        gate.set_track_phase("futures_day", SessionPhase.OPEN)

        assert gate.get_phase("MXFH6") == SessionPhase.CLOSED

    def test_option_series_does_not_match_a_futures_root(self) -> None:
        """A root match is root + month letter + year digit, not a prefix scan.

        TXO codes are longer, so a loose ``startswith`` would have swept the
        whole option chain into the futures track.
        """
        gate = TrackGate()
        gate.register_root("TXF", "futures_day")
        gate.set_track_phase("futures_day", SessionPhase.OPEN)

        assert gate.get_phase("TXFH6") == SessionPhase.OPEN
        for option_code in ("TXO20800L6", "TXO45000H6", "TXFH6X"):
            assert gate.get_phase(option_code) == SessionPhase.CLOSED

    def test_root_resolution_is_memoised(self) -> None:
        """get_phase runs on the intent path; the scan must happen once."""
        gate = TrackGate()
        gate.register_root("TMF", "futures_day")
        gate.set_track_phase("futures_day", SessionPhase.OPEN)

        assert "TMFH6" not in gate.symbol_to_track
        gate.get_phase("TMFH6")
        assert gate.symbol_to_track["TMFH6"] == ["futures_day"]

    def test_memoised_entry_does_not_alias_the_root_track_list(self) -> None:
        """Mutating a resolved symbol's tracks must not corrupt the root."""
        gate = TrackGate()
        gate.register_root("TMF", "futures_day")
        gate.set_track_phase("futures_day", SessionPhase.OPEN)
        gate.set_track_phase("futures_night", SessionPhase.CLOSED)

        gate.get_phase("TMFH6")
        gate.register_symbol("TMFH6", "futures_night")

        # TMFI6 resolves through the same root and must not have inherited
        # futures_night from TMFH6's registration.
        assert gate.symbol_to_track["TMFH6"] == ["futures_day", "futures_night"]
        gate.get_phase("TMFI6")
        assert gate.symbol_to_track["TMFI6"] == ["futures_day"]

    def test_multi_track_root_returns_most_permissive_phase(self) -> None:
        gate = TrackGate()
        gate.register_root("TMF", "futures_day")
        gate.register_root("TMF", "futures_night")
        gate.set_track_phase("futures_day", SessionPhase.OPEN)
        gate.set_track_phase("futures_night", SessionPhase.CLOSED)

        assert gate.get_phase("TMFH6") == SessionPhase.OPEN


@pytest.mark.unit
class TestGovernorLoadsRoots:
    def test_roots_from_yaml_reach_the_gate(self, tmp_path: Path) -> None:
        cfg = tmp_path / "session_governor.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {
                    "tracks": {
                        "futures_day": {
                            "roots": ["TMF", "TXF"],
                            "symbols": ["TMFR1"],
                            "schedule": [{"phase": "open", "time": "08:45"}],
                        }
                    }
                }
            )
        )
        gov = SessionGovernor(config_path=cfg)
        gov.track_gate.set_track_phase("futures_day", SessionPhase.OPEN)

        assert gov.track_gate.get_phase("TMFH6") == SessionPhase.OPEN
        assert gov.track_gate.get_phase("TXFH6") == SessionPhase.OPEN

    def test_config_without_roots_still_loads(self, tmp_path: Path) -> None:
        """`roots` is additive: pre-existing configs must keep working."""
        cfg = tmp_path / "session_governor.yaml"
        cfg.write_text(
            yaml.safe_dump(
                {
                    "tracks": {
                        "stock": {
                            "symbols": ["2330"],
                            "schedule": [{"phase": "open", "time": "09:00"}],
                        }
                    }
                }
            )
        )
        gov = SessionGovernor(config_path=cfg)
        gov.track_gate.set_track_phase("stock", SessionPhase.OPEN)

        assert gov.track_gate.get_phase("2330") == SessionPhase.OPEN


@pytest.mark.unit
class TestLiveConfigCannotGoStale:
    """Guards on the shipped file — this class is what would have caught the bug.

    The unit tests above all pass against a hand-built gate; the outage lived
    entirely in the config, which no test read.
    """

    @staticmethod
    def _tracks() -> dict:
        return (yaml.safe_load(_LIVE_CONFIG.read_text()) or {}).get("tracks", {})

    def test_no_track_names_a_month_code(self) -> None:
        """A month code expires. Anything that expires cannot define membership."""
        offenders: list[str] = []
        for track_name, track in self._tracks().items():
            for symbol in track.get("symbols", []):
                # <3-letter root><month letter><year digit>, e.g. TMFE6.
                if len(symbol) == 5 and symbol[3] in _MONTH_LETTERS and symbol[4].isdigit():
                    offenders.append(f"{track_name}:{symbol}")
        assert not offenders, (
            f"session_governor.yaml names month codes {offenders}; they expire and the track "
            "silently stops covering the live contract. Declare the product root instead."
        )

    def test_every_futures_track_declares_roots(self) -> None:
        tracks = self._tracks()
        assert tracks, "live session_governor.yaml declares no tracks"
        for track_name, track in tracks.items():
            if not track_name.startswith("futures"):
                continue
            assert track.get("roots"), f"futures track {track_name!r} declares no roots"

    def test_live_config_covers_the_traded_product_roots(self) -> None:
        """TMF (Mini-TAIEX, what R47 trades) and TXF must resolve in both sessions."""
        gov = SessionGovernor(config_path=_LIVE_CONFIG)
        gate = gov.track_gate
        for track_name in ("futures_day", "futures_night"):
            gate.set_track_phase(track_name, SessionPhase.CLOSED)

        for track_name in ("futures_day", "futures_night"):
            gate.set_track_phase(track_name, SessionPhase.OPEN)
            for symbol in ("TMFH6", "TXFH6", "TMFI6", "TXFI6"):
                assert gate.get_phase(symbol) == SessionPhase.OPEN, (
                    f"{symbol} is not covered by {track_name} in the live config"
                )
            gate.set_track_phase(track_name, SessionPhase.CLOSED)
