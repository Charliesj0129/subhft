"""Tests for ReportRenderer — free/paid tier message generation."""

from __future__ import annotations

from hft_platform.reports.models import (
    FlowBar,
    KeyLevel,
    PriceLevel,
    Scenario,
    ScenarioReport,
    SessionData,
    SignalReport,
)
from hft_platform.reports.renderer import ReportRenderer


def _make_report() -> ScenarioReport:
    fb = FlowBar(
        ts="2026-03-27 21:50:00",
        ticks=737,
        total_vol=1122,
        uptick_vol=288,
        downtick_vol=540,
        flat_vol=294,
        ud_ratio=0.533,
        net_flow=-252,
    )
    fb_buy = FlowBar(
        ts="2026-03-27 23:00:00",
        ticks=463,
        total_vol=763,
        uptick_vol=373,
        downtick_vol=206,
        flat_vol=184,
        ud_ratio=1.811,
        net_flow=167,
    )
    sd = SessionData(
        session="night",
        symbol="TXFD6",
        date="2026-03-27",
        open=330490000,
        high=330490000,
        low=323750000,
        close=324380000,
        volume=58107,
        tick_count=38153,
        bars_5m=[],
        flow_5m=[fb] * 20,
        large_trades=[],
        spread_dist={3: 147202, 4: 81011},
        depth_imbalance=[],
    )
    signal = SignalReport(
        session_data=sd,
        total_net_flow=-1581,
        ud_ratio_session=0.906,
        strongest_sell=fb,
        strongest_buy=fb_buy,
        large_buy_volume=380,
        large_sell_volume=650,
        large_net=-270,
        key_large_trades=[],
        supports=[
            PriceLevel(price=323750000, strength=0.9, reason="雙底"),
            PriceLevel(price=320000000, strength=0.6, reason="整千關卡"),
        ],
        resistances=[
            PriceLevel(price=327500000, strength=0.9, reason="反彈天花板"),
            PriceLevel(price=330000000, strength=0.7, reason="被砸穿"),
        ],
        bias="bearish",
        bias_confidence=0.75,
        rule_scores={},
    )
    return ScenarioReport(
        signal=signal,
        direction="偏空",
        confidence_pct=75,
        entry_zone=(327000000, 327500000),
        target=323750000,
        stop_loss=328500000,
        scenarios=[
            Scenario(
                id="break",
                label="破底加速",
                probability="較高",
                condition="若破 32,375",
                target=320000000,
                description="目標看 32,000",
            ),
            Scenario(
                id="bounce",
                label="守底反彈",
                probability="較低",
                condition="若守住 32,375",
                target=327500000,
                description="目標看 32,750",
            ),
        ],
        key_levels=[
            KeyLevel(price=323750000, label="S1", importance=3, reason="雙底"),
            KeyLevel(price=327500000, label="R1", importance=3, reason="反彈天花板"),
        ],
    )


# ---------------------------------------------------------------------------
# Message count
# ---------------------------------------------------------------------------


def test_paid_returns_five_messages() -> None:
    renderer = ReportRenderer()
    report = _make_report()
    msgs = renderer.render(report, tier="paid")
    assert len(msgs) == 5


def test_free_returns_three_messages() -> None:
    renderer = ReportRenderer()
    report = _make_report()
    msgs = renderer.render(report, tier="free")
    assert len(msgs) == 3


# ---------------------------------------------------------------------------
# Message length
# ---------------------------------------------------------------------------


def test_all_messages_within_telegram_limit_paid() -> None:
    renderer = ReportRenderer()
    msgs = renderer.render(_make_report(), tier="paid")
    for i, msg in enumerate(msgs):
        assert len(msg) <= 4096, f"Message {i} exceeds 4096 chars (len={len(msg)})"


def test_all_messages_within_telegram_limit_free() -> None:
    renderer = ReportRenderer()
    msgs = renderer.render(_make_report(), tier="free")
    for i, msg in enumerate(msgs):
        assert len(msg) <= 4096, f"Message {i} exceeds 4096 chars (len={len(msg)})"


# ---------------------------------------------------------------------------
# Content assertions — paid tier
# ---------------------------------------------------------------------------


def test_paid_contains_key_level_s1() -> None:
    renderer = ReportRenderer()
    msgs = renderer.render(_make_report(), tier="paid")
    full = "\n".join(msgs)
    assert "S1" in full
    assert "32,375" in full


def test_paid_contains_scenarios() -> None:
    renderer = ReportRenderer()
    msgs = renderer.render(_make_report(), tier="paid")
    full = "\n".join(msgs)
    assert "破底加速" in full
    assert "守底反彈" in full


# ---------------------------------------------------------------------------
# Content assertions — free tier (no levels)
# ---------------------------------------------------------------------------


def test_free_does_not_contain_s1() -> None:
    renderer = ReportRenderer()
    msgs = renderer.render(_make_report(), tier="free")
    full = "\n".join(msgs)
    assert "S1" not in full


# ---------------------------------------------------------------------------
# Disclaimer always present
# ---------------------------------------------------------------------------


def test_disclaimer_present_in_paid() -> None:
    renderer = ReportRenderer()
    msgs = renderer.render(_make_report(), tier="paid")
    assert any("投資有風險" in m for m in msgs)


def test_disclaimer_present_in_free() -> None:
    renderer = ReportRenderer()
    msgs = renderer.render(_make_report(), tier="free")
    assert any("投資有風險" in m for m in msgs)


# ---------------------------------------------------------------------------
# Summary OHLC prices
# ---------------------------------------------------------------------------


def test_summary_contains_ohlc_prices() -> None:
    renderer = ReportRenderer()
    msgs = renderer.render(_make_report(), tier="paid")
    summary = msgs[0]
    # open=33,049, low=32,375
    assert "33,049" in summary
    assert "32,375" in summary


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------


def test_p_formats_price() -> None:
    from hft_platform.reports.renderer import _p

    assert _p(330490000) == "33,049"
    assert _p(323750000) == "32,375"
    assert _p(10000) == "1"


def test_pct_decline() -> None:
    from hft_platform.reports.renderer import _pct

    result = _pct(330490000, 324380000)
    assert result.startswith("▼")
    assert "-1.85%" in result


def test_pct_advance() -> None:
    from hft_platform.reports.renderer import _pct

    result = _pct(320000000, 330000000)
    assert result.startswith("▲")


def test_stars_three() -> None:
    from hft_platform.reports.renderer import _stars

    assert _stars(3) == "★★★"


def test_stars_two() -> None:
    from hft_platform.reports.renderer import _stars

    assert _stars(2) == "★★☆"


def test_stars_one() -> None:
    from hft_platform.reports.renderer import _stars

    assert _stars(1) == "★☆☆"


def test_ud_bar_returns_string() -> None:
    from hft_platform.reports.renderer import _ud_bar

    assert isinstance(_ud_bar(0.5), str)
    assert isinstance(_ud_bar(1.0), str)
    assert isinstance(_ud_bar(2.0), str)
