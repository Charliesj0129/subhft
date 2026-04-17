"""Regression tests for Bug 21 (2026-04-17 R47 TMFE6 deadlock).

Symptom: R47 held short position -1 TMFE6 at 37478. Price moved +242pt against
the short. PEAK_DRAWDOWN protection kicked in and rejected every cover BUY
intent (side=0), creating a deadlock where the strategy couldn't exit.

Log evidence:
  PEAK_DRAWDOWN: drawdown=285000 > limit=112000  side=0 (BUY to cover)
  PEAK_DRAWDOWN: drawdown=1955000 > limit=112000 side=0
  PEAK_DRAWDOWN: drawdown=2120000 > limit=112000 side=0
  ... continuous rejections
User had to manually cover via broker IOC, realizing -2680 NTD loss that could
have been smaller if the strategy had been allowed to cover.

Root cause: Risk validators distinguish only CANCEL/FORCE_FLAT from everything
else. NEW intents that REDUCE absolute position (covers, partial exits) are
treated identically to opening orders and blocked by the same safety gates.

Fix: Add `reduces_position(intent)` predicate to RiskValidator base. When the
order strictly reduces absolute position magnitude, bypass DailyLossLimit,
MaxNotional, and PerSymbolNotional validators. PositionLimit is intrinsically
safe (checks resulting_qty) and needs no change. Overshooting (flip sign and
grow) is correctly NOT classified as reducing.
"""

from __future__ import annotations

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.risk.validators import (
    DailyLossLimitValidator,
    MaxNotionalValidator,
    PerSymbolNotionalValidator,
    RiskValidator,
)


def _make_intent(side=Side.BUY, qty=1, price=374780000, strategy_id="R47", symbol="TMFE6"):
    return OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=IntentType.NEW,
        side=side,
        price=price,
        qty=qty,
    )


def _position_provider(positions: dict[tuple[str, str], int]):
    def _fn(symbol: str, strategy_id: str) -> int:
        return positions.get((symbol, strategy_id), 0)

    return _fn


# ---------------------------------------------------------------------------
# 1. The reduces_position() predicate itself
# ---------------------------------------------------------------------------
class TestReducesPositionPredicate:
    def _mk(self, pos: int) -> RiskValidator:
        positions = {("TMFE6", "R47"): pos}
        return RiskValidator({}, None, position_provider=_position_provider(positions))

    def test_cover_short_exact_amount(self):
        """SELL -1 short, BUY 1 cover → new=0, |0|<|-1| → reduces."""
        v = self._mk(-1)
        assert v.reduces_position(_make_intent(side=Side.BUY, qty=1)) is True

    def test_cover_long_exact_amount(self):
        """BUY +1 long, SELL 1 cover → new=0, |0|<|+1| → reduces."""
        v = self._mk(+1)
        assert v.reduces_position(_make_intent(side=Side.SELL, qty=1)) is True

    def test_partial_cover_short(self):
        """Short -3, BUY 1 partial cover → new=-2, |-2|<|-3| → reduces."""
        v = self._mk(-3)
        assert v.reduces_position(_make_intent(side=Side.BUY, qty=1)) is True

    def test_overshoot_flip_same_magnitude_does_not_reduce(self):
        """Short -1, BUY 2 → new=+1, |+1|=|-1| → NOT reducing (flip-and-hold)."""
        v = self._mk(-1)
        assert v.reduces_position(_make_intent(side=Side.BUY, qty=2)) is False

    def test_overshoot_flip_larger_does_not_reduce(self):
        """Short -1, BUY 3 → new=+2, |+2|>|-1| → NOT reducing (over-flip)."""
        v = self._mk(-1)
        assert v.reduces_position(_make_intent(side=Side.BUY, qty=3)) is False

    def test_flat_position_no_reduction(self):
        """Flat 0, BUY 1 → new=+1, |+1|>|0| → NOT reducing (pure open)."""
        v = self._mk(0)
        assert v.reduces_position(_make_intent(side=Side.BUY, qty=1)) is False

    def test_add_to_short_does_not_reduce(self):
        """Short -1, SELL 1 → new=-2, |-2|>|-1| → NOT reducing (grow short)."""
        v = self._mk(-1)
        assert v.reduces_position(_make_intent(side=Side.SELL, qty=1)) is False

    def test_add_to_long_does_not_reduce(self):
        """Long +1, BUY 1 → new=+2, |+2|>|+1| → NOT reducing (grow long)."""
        v = self._mk(+1)
        assert v.reduces_position(_make_intent(side=Side.BUY, qty=1)) is False

    def test_cancel_always_reduces(self):
        v = self._mk(-1)
        cancel = OrderIntent(
            intent_id=1,
            strategy_id="R47",
            symbol="TMFE6",
            intent_type=IntentType.CANCEL,
            side=Side.BUY,
            price=0,
            qty=0,
        )
        assert v.reduces_position(cancel) is True

    def test_force_flat_always_reduces(self):
        v = self._mk(-1)
        ff = OrderIntent(
            intent_id=1,
            strategy_id="R47",
            symbol="TMFE6",
            intent_type=IntentType.FORCE_FLAT,
            side=Side.BUY,
            price=0,
            qty=1,
        )
        assert v.reduces_position(ff) is True

    def test_no_position_provider_returns_false_for_new(self):
        """Without position_provider, can't determine — conservative default: don't bypass."""
        v = RiskValidator({}, None)
        assert v.reduces_position(_make_intent(side=Side.BUY, qty=1)) is False


# ---------------------------------------------------------------------------
# 2. DailyLossLimitValidator — PEAK_DRAWDOWN, SOFT_LIMIT, DAILY_LOSS_LIMIT
# ---------------------------------------------------------------------------
class TestDailyLossLimitCoverBypass:
    def _make_validator(self, position: int = 0):
        cfg = {
            "global_defaults": {"max_daily_loss": 1_000_000},
            "intraday_pnl": {
                "soft_limit_ntd": 500,
                "hard_limit_ntd": 8000,
                "peak_drawdown_pct": 0.40,
                "soft_recovery_ntd": 300,
                "drawdown_recovery_pct": 0.20,
                "soft_limit_cooldown_s": 60,
                "peak_drawdown_min_peak_ntd": 200,
                "price_scale": 10000,
                "point_value": 10,
            },
        }
        positions = {("TMFE6", "R47"): position}
        return DailyLossLimitValidator(cfg, None, position_provider=_position_provider(positions))

    def test_peak_drawdown_blocks_opener_still(self):
        """Sanity: opener SHOULD still be blocked by PEAK_DRAWDOWN."""
        v = self._make_validator(position=0)
        v.record_pnl("R47", 280_000)  # +280 NTD gain establishes peak
        v.update_unrealized(0)
        v.check(_make_intent(side=Side.BUY, qty=1))  # update peak via check
        v.update_unrealized(-2_120_000)  # -2120 NTD unrealized => drawdown huge
        ok, reason = v.check(_make_intent(side=Side.BUY, qty=1))
        assert not ok, "Opener must still be blocked — drawdown protection required"
        assert "PEAK_DRAWDOWN" in reason

    def test_peak_drawdown_allows_cover_when_short(self):
        """Bug 21: cover order for short position must bypass PEAK_DRAWDOWN."""
        v = self._make_validator(position=-1)  # R47 is short 1 lot
        v.record_pnl("R47", 280_000)
        v.update_unrealized(0)
        v.check(_make_intent(side=Side.BUY, qty=1))  # prime peak
        v.update_unrealized(-2_120_000)  # large unrealized loss

        # Cover BUY must be allowed
        ok, reason = v.check(_make_intent(side=Side.BUY, qty=1))
        assert ok, f"Cover order must bypass PEAK_DRAWDOWN, got: {reason}"
        assert reason in ("OK", "REDUCE_ONLY_BYPASS")

    def test_peak_drawdown_allows_cover_when_long(self):
        """Long position in drawdown must be allowed to close."""
        v = self._make_validator(position=+1)
        v.record_pnl("R47", 280_000)
        v.update_unrealized(0)
        v.check(_make_intent(side=Side.SELL, qty=1))
        v.update_unrealized(-2_120_000)

        ok, reason = v.check(_make_intent(side=Side.SELL, qty=1))
        assert ok, f"Long exit must bypass PEAK_DRAWDOWN, got: {reason}"

    def test_peak_drawdown_blocks_overshoot_flip(self):
        """Short -1, BUY 2 (flip to +1): same magnitude, NOT reducing → still blocked."""
        v = self._make_validator(position=-1)
        v.record_pnl("R47", 280_000)
        v.update_unrealized(0)
        v.check(_make_intent(side=Side.BUY, qty=1))
        v.update_unrealized(-2_120_000)

        ok, reason = v.check(_make_intent(side=Side.BUY, qty=2))
        assert not ok, "Flip-and-hold must still be blocked (not a reducing order)"

    def test_soft_limit_allows_cover(self):
        """SOFT_LIMIT must also allow covers (Bug 21 extended)."""
        v = self._make_validator(position=-1)
        v.record_pnl("R47", -600_000)  # -600 NTD realized → soft limit breached
        v.update_unrealized(0)
        ok, reason = v.check(_make_intent(side=Side.BUY, qty=1))
        assert ok, f"Cover under SOFT_LIMIT must bypass, got: {reason}"

    def test_daily_loss_limit_allows_cover(self):
        """Hard DAILY_LOSS_LIMIT_EXCEEDED must also allow covers."""
        v = self._make_validator(position=-1)
        v.record_pnl("R47", -9_000_000)  # -9000 NTD > 8000 hard limit
        v.update_unrealized(0)
        ok, reason = v.check(_make_intent(side=Side.BUY, qty=1))
        assert ok, f"Cover under DAILY_LOSS_LIMIT must bypass, got: {reason}"

    def test_daily_loss_limit_blocks_opener(self):
        """Opener under DAILY_LOSS_LIMIT_EXCEEDED must still be blocked."""
        v = self._make_validator(position=0)
        v.record_pnl("R47", -9_000_000)
        ok, reason = v.check(_make_intent(side=Side.BUY, qty=1))
        assert not ok


# ---------------------------------------------------------------------------
# 3. MaxNotionalValidator — cover bypass
# ---------------------------------------------------------------------------
class TestMaxNotionalCoverBypass:
    def _make(self, position: int = 0):
        cfg = {
            "global_defaults": {"max_notional": 100},  # tiny cap to force rejection
            "strategies": {},
        }
        positions = {("TMFE6", "R47"): position}
        return MaxNotionalValidator(cfg, None, position_provider=_position_provider(positions))

    def test_blocks_opener(self):
        v = self._make(position=0)
        ok, _ = v.check(_make_intent(price=374780000, qty=1))  # huge notional
        assert not ok

    def test_allows_cover_short(self):
        v = self._make(position=-1)
        ok, reason = v.check(_make_intent(side=Side.BUY, price=374780000, qty=1))
        assert ok, f"Cover BUY under notional cap must bypass, got: {reason}"

    def test_blocks_overshoot(self):
        v = self._make(position=-1)
        ok, _ = v.check(_make_intent(side=Side.BUY, price=374780000, qty=3))
        assert not ok


# ---------------------------------------------------------------------------
# 4. PerSymbolNotionalValidator — cover bypass
# ---------------------------------------------------------------------------
class TestStormGuardReduceOnly:
    """Bug 21: StormGuard HALT/STORM must allow cover orders through."""

    def _make(self, position: int, state: str = "HALT"):
        from hft_platform.contracts.strategy import StormGuardState
        from hft_platform.risk.storm_guard import StormGuard

        guard = StormGuard()
        guard.set_position_provider(_position_provider({("TMFE6", "R47"): position}))
        guard.state = StormGuardState.HALT if state == "HALT" else StormGuardState.STORM
        return guard

    def test_halt_blocks_opener(self):
        guard = self._make(position=0, state="HALT")
        ok, reason = guard.validate(_make_intent(side=Side.BUY, qty=1))
        assert not ok
        assert reason == "STORMGUARD_HALT"

    def test_halt_allows_cover_short(self):
        guard = self._make(position=-1, state="HALT")
        ok, reason = guard.validate(_make_intent(side=Side.BUY, qty=1))
        assert ok, f"HALT must allow cover, got {reason}"
        assert reason == "HALT_REDUCE_ONLY"

    def test_halt_blocks_overshoot_flip(self):
        guard = self._make(position=-1, state="HALT")
        ok, _ = guard.validate(_make_intent(side=Side.BUY, qty=2))
        assert not ok, "Flip-and-hold under HALT must still be blocked"

    def test_storm_blocks_opener(self):
        guard = self._make(position=0, state="STORM")
        ok, reason = guard.validate(_make_intent(side=Side.BUY, qty=1))
        assert not ok
        assert reason == "STORMGUARD_STORM_BLOCKED"

    def test_storm_allows_cover_long(self):
        guard = self._make(position=+1, state="STORM")
        ok, reason = guard.validate(_make_intent(side=Side.SELL, qty=1))
        assert ok, f"STORM must allow long exit, got {reason}"
        assert reason == "STORM_REDUCE_ONLY"

    def test_cancel_always_passes_in_halt(self):
        guard = self._make(position=0, state="HALT")
        cancel = OrderIntent(
            intent_id=1,
            strategy_id="R47",
            symbol="TMFE6",
            intent_type=IntentType.CANCEL,
            side=Side.BUY,
            price=0,
            qty=0,
        )
        ok, _ = guard.validate(cancel)
        assert ok


class TestPerSymbolNotionalCoverBypass:
    def _make(self, position: int = 0):
        cfg = {"global_defaults": {"per_symbol_max_notional": 100}}
        positions = {("TMFE6", "R47"): position}
        return PerSymbolNotionalValidator(cfg, None, position_provider=_position_provider(positions))

    def test_blocks_opener(self):
        v = self._make(position=0)
        ok, _ = v.check(_make_intent(price=374780000, qty=1))
        assert not ok

    def test_allows_cover(self):
        v = self._make(position=-1)
        ok, reason = v.check(_make_intent(side=Side.BUY, price=374780000, qty=1))
        assert ok, f"Cover under per-symbol notional cap must bypass, got: {reason}"
