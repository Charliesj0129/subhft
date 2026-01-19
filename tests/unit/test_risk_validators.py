from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side, StormGuardState
from hft_platform.risk.validators import MaxNotionalValidator, PriceBandValidator, StormGuardFSM


def _intent(price: int, qty: int, intent_type=IntentType.NEW):
    return OrderIntent(
        intent_id=1,
        strategy_id="strat",
        symbol="AAA",
        intent_type=intent_type,
        side=Side.BUY,
        price=price,
        qty=qty,
        tif=TIF.LIMIT,
        target_order_id=None,
        timestamp_ns=0,
    )


def test_price_band_validator():
    cfg = {"global_defaults": {"max_price_cap": 1.0}}
    validator = PriceBandValidator(cfg)

    ok, _ = validator.check(_intent(0, 1))
    assert not ok

    ok, _ = validator.check(_intent(10000, 1))
    assert ok

    ok, reason = validator.check(_intent(20000, 1))
    assert not ok
    assert "PRICE_EXCEEDS_CAP" in reason

    ok, _ = validator.check(_intent(0, 1, intent_type=IntentType.CANCEL))
    assert ok


def test_max_notional_validator():
    cfg = {"global_defaults": {"max_notional": 5}}
    validator = MaxNotionalValidator(cfg)

    ok, _ = validator.check(_intent(10000, 3))
    assert ok

    ok, reason = validator.check(_intent(10000, 10))
    assert not ok
    assert "MAX_NOTIONAL_EXCEEDED" in reason


def test_max_notional_per_strategy_override():
    cfg = {"global_defaults": {"max_notional": 100}, "strategies": {"strat": {"max_notional": 5}}}
    validator = MaxNotionalValidator(cfg)

    ok, reason = validator.check(_intent(10000, 10))
    assert not ok
    assert "MAX_NOTIONAL_EXCEEDED" in reason


def test_max_notional_cancel_ok():
    cfg = {"global_defaults": {"max_notional": 1}}
    validator = MaxNotionalValidator(cfg)

    ok, _ = validator.check(_intent(10000, 100, intent_type=IntentType.CANCEL))
    assert ok


def test_price_band_default_cap():
    validator = PriceBandValidator({})

    ok, _ = validator.check(_intent(5_000 * 10000, 1))
    assert ok

    ok, reason = validator.check(_intent(5_001 * 10000, 1))
    assert not ok
    assert "PRICE_EXCEEDS_CAP" in reason


def test_storm_guard_fsm_transitions():
    cfg = {"storm_guard": {"warm_threshold": -10, "storm_threshold": -20, "halt_threshold": -30}}
    fsm = StormGuardFSM(cfg)

    fsm.update_pnl(-5)
    assert fsm.state == StormGuardState.NORMAL

    fsm.update_pnl(-15)
    assert fsm.state == StormGuardState.WARM

    fsm.update_pnl(-25)
    assert fsm.state == StormGuardState.STORM

    fsm.update_pnl(-35)
    assert fsm.state == StormGuardState.HALT

    ok, _ = fsm.validate(_intent(10000, 1, intent_type=IntentType.CANCEL))
    assert ok

    ok, reason = fsm.validate(_intent(10000, 1, intent_type=IntentType.NEW))
    assert not ok
    assert reason == "STORMGUARD_HALT"


def test_price_band_respects_symbol_scale(tmp_path, monkeypatch):
    symbols_cfg = tmp_path / "symbols.yaml"
    symbols_cfg.write_text("symbols:\n  - code: 'AAA'\n    exchange: 'TSE'\n    price_scale: 100\n")
    monkeypatch.setenv("SYMBOLS_CONFIG", str(symbols_cfg))

    cfg = {"global_defaults": {"max_price_cap": 1.0}}
    validator = PriceBandValidator(cfg)

    ok, reason = validator.check(_intent(150, 1))
    assert not ok
    assert "PRICE_EXCEEDS_CAP" in reason


def test_max_notional_respects_symbol_scale(tmp_path, monkeypatch):
    symbols_cfg = tmp_path / "symbols.yaml"
    symbols_cfg.write_text("symbols:\n  - code: 'AAA'\n    exchange: 'TSE'\n    price_scale: 100\n")
    monkeypatch.setenv("SYMBOLS_CONFIG", str(symbols_cfg))

    cfg = {"global_defaults": {"max_notional": 2.0}}
    validator = MaxNotionalValidator(cfg)

    ok, reason = validator.check(_intent(150, 2))
    assert not ok
    assert "MAX_NOTIONAL_EXCEEDED" in reason
