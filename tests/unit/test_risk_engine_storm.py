import asyncio

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side, StormGuardState
from hft_platform.risk.engine import RiskEngine


def _intent(intent_type):
    return OrderIntent(
        intent_id=1,
        strategy_id="strat",
        symbol="AAA",
        intent_type=intent_type,
        side=Side.BUY,
        price=10000,
        qty=1,
        tif=TIF.LIMIT,
        target_order_id=None,
        timestamp_ns=0,
    )


@pytest.mark.asyncio
async def test_risk_engine_storm_blocks_new(tmp_path):
    cfg = tmp_path / "risk.yaml"
    cfg.write_text(
        "\n".join(
            [
                "global_defaults:",
                "  max_notional: 1000000",
                "  max_price_cap: 100000",
                "storm_guard:",
                "  warm_threshold: -10",
                "  storm_threshold: -20",
                "  halt_threshold: -30",
            ]
        )
        + "\n"
    )
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    engine = RiskEngine(str(cfg), q_in, q_out)
    engine.storm_guard.state = StormGuardState.STORM

    decision = engine.evaluate(_intent(IntentType.NEW))
    assert decision.approved is False
    assert decision.reason_code == "STORMGUARD_STORM_NEW_BLOCKED"


@pytest.mark.asyncio
async def test_risk_engine_halt_allows_cancel(tmp_path):
    cfg = tmp_path / "risk.yaml"
    cfg.write_text("global_defaults:\n  max_notional: 1000000\n  max_price_cap: 100000\n")
    q_in = asyncio.Queue()
    q_out = asyncio.Queue()
    engine = RiskEngine(str(cfg), q_in, q_out)
    engine.storm_guard.state = StormGuardState.HALT

    decision = engine.evaluate(_intent(IntentType.CANCEL))
    assert decision.approved is True
