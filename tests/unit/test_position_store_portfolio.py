"""Unit tests for PositionStore portfolio tracking: total_pnl, peak equity, and drawdown.

All prices/PnL values use scaled integers (x10000) — no floats in financial calculations.
Drawdown percentage is a display-only float, which is the expected and correct form.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hft_platform.contracts.execution import FillEvent, Side
from hft_platform.execution.positions import PositionStore


def _make_fill(
    side: Side,
    qty: int,
    price: int,
    fee: int = 0,
    tax: int = 0,
    account_id: str = "ACC",
    strategy_id: str = "STRAT",
    symbol: str = "SYM",
    ts: int = 0,
) -> FillEvent:
    """Helper to construct a FillEvent with scaled-integer price."""
    return FillEvent(
        fill_id="F1",
        account_id=account_id,
        order_id="O1",
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=fee,
        tax=tax,
        ingest_ts_ns=0,
        match_ts_ns=ts,
    )


@pytest.fixture(autouse=True)
def _lower_drawdown_threshold(monkeypatch):
    """Lower _MIN_PEAK_SCALED so legacy small-PnL fixtures still exercise
    drawdown semantics. Production default 100M is calibrated for live HFT
    scale and masks unit-test fixture values."""
    import hft_platform.execution.positions as _positions

    monkeypatch.setattr(_positions, "_MIN_PEAK_SCALED", 2_000_000)


@pytest.fixture()
def store():
    """PositionStore with metrics disabled for unit testing."""
    s = PositionStore()
    s.metrics = None
    return s


def test_get_drawdown_pct_no_fills(store):
    """Empty store should return 0.0 — no peak, no drawdown."""
    assert store.get_drawdown_pct() == 0.0
    assert store.total_pnl == 0


def test_get_drawdown_pct_at_peak(store):
    """After a profit fill with no subsequent loss, drawdown must be 0.0."""
    store.on_fill(_make_fill(Side.BUY, 10, 1_000_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_005_000))

    assert store.total_pnl == 50_000
    assert store.get_drawdown_pct() == 0.0


def test_get_drawdown_pct_after_profit_then_loss(store):
    """After profit then loss, drawdown fraction must be positive and <= 1.0."""
    store.on_fill(_make_fill(Side.BUY, 10, 1_000_000, symbol="SYM_A"))
    store.on_fill(_make_fill(Side.SELL, 10, 1_300_000, symbol="SYM_A"))
    assert store.total_pnl == 3_000_000
    assert store.get_drawdown_pct() == 0.0

    store.on_fill(_make_fill(Side.BUY, 10, 1_300_000, symbol="SYM_A"))
    store.on_fill(_make_fill(Side.SELL, 10, 1_000_000, symbol="SYM_A"))
    assert store.total_pnl == 0

    dd = store.get_drawdown_pct()
    assert dd == pytest.approx(1.0, rel=1e-6)


def test_get_drawdown_pct_partial_drawdown(store):
    """Partial drawdown: lose half the peak equity."""
    store.on_fill(_make_fill(Side.BUY, 10, 1_000_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_300_000))
    assert store.total_pnl == 3_000_000

    store.on_fill(_make_fill(Side.BUY, 10, 1_300_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_150_000))
    assert store.total_pnl == 1_500_000

    dd = store.get_drawdown_pct()
    assert dd == pytest.approx(0.5, rel=1e-6)


def test_total_pnl_aggregates_across_positions(store):
    """total_pnl must be the sum of realized PnL across all symbol positions."""
    store.on_fill(_make_fill(Side.BUY, 5, 1_000_000, symbol="SYM_A"))
    store.on_fill(_make_fill(Side.SELL, 5, 1_010_000, symbol="SYM_A"))

    store.on_fill(_make_fill(Side.BUY, 10, 2_000_000, symbol="SYM_B"))
    store.on_fill(_make_fill(Side.SELL, 10, 2_005_000, symbol="SYM_B"))

    store.on_fill(_make_fill(Side.BUY, 3, 500_000, symbol="SYM_C"))
    store.on_fill(_make_fill(Side.SELL, 3, 490_000, symbol="SYM_C"))

    expected_total = 50_000 + 50_000 + (-30_000)
    assert store.total_pnl == expected_total

    pnl_a = store.positions["ACC:STRAT:SYM_A"].realized_pnl_scaled
    pnl_b = store.positions["ACC:STRAT:SYM_B"].realized_pnl_scaled
    pnl_c = store.positions["ACC:STRAT:SYM_C"].realized_pnl_scaled
    assert pnl_a + pnl_b + pnl_c == expected_total


def test_peak_equity_tracking(store):
    """Peak equity must track the high watermark and never decrease."""
    store.on_fill(_make_fill(Side.BUY, 10, 1_000_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_010_000))
    peak_after_step1 = store._peak_equity_scaled
    assert peak_after_step1 == 100_000

    store.on_fill(_make_fill(Side.BUY, 10, 1_010_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_005_000))
    assert store._peak_equity_scaled == peak_after_step1

    store.on_fill(_make_fill(Side.BUY, 10, 1_005_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_020_000))
    assert store._peak_equity_scaled > peak_after_step1


def test_peak_equity_never_negative_start(store):
    """If first trades are losing, peak stays 0 and drawdown reports loss-based fraction."""
    store.on_fill(_make_fill(Side.BUY, 10, 1_010_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_000_000))

    assert store.total_pnl == -100_000
    assert store._peak_equity_scaled == 0
    assert store.get_drawdown_pct() == 0.0


def test_drawdown_resets_on_new_peak(store):
    """Drawdown should return to 0.0 when equity recovers to a new peak."""
    store.on_fill(_make_fill(Side.BUY, 10, 1_000_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_300_000))
    assert store.get_drawdown_pct() == 0.0

    store.on_fill(_make_fill(Side.BUY, 10, 1_300_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_150_000))
    assert store.get_drawdown_pct() > 0.0

    store.on_fill(_make_fill(Side.BUY, 10, 1_150_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_450_000))
    assert store.get_drawdown_pct() == 0.0


def test_multiple_strategies_independent_positions(store):
    """Fills for different strategy IDs must produce independent positions
    that all contribute to the portfolio total_pnl."""
    store.on_fill(_make_fill(Side.BUY, 10, 1_000_000, strategy_id="ALPHA"))
    store.on_fill(_make_fill(Side.SELL, 10, 1_010_000, strategy_id="ALPHA"))

    store.on_fill(_make_fill(Side.BUY, 10, 1_000_000, strategy_id="BETA"))
    store.on_fill(_make_fill(Side.SELL, 10, 990_000, strategy_id="BETA"))

    pnl_alpha = store.positions["ACC:ALPHA:SYM"].realized_pnl_scaled
    pnl_beta = store.positions["ACC:BETA:SYM"].realized_pnl_scaled

    assert pnl_alpha == 100_000
    assert pnl_beta == -100_000
    assert store.total_pnl == 0


def _build_drawdown(store) -> None:
    """Take the store to a peak, then give part of it back."""
    store.on_fill(_make_fill(Side.BUY, 10, 1_000_000))
    store.on_fill(_make_fill(Side.SELL, 10, 1_500_000))
    store.on_fill(_make_fill(Side.BUY, 10, 1_000_000))
    store.on_fill(_make_fill(Side.SELL, 10, 900_000))


def test_peak_equity_rebases_at_the_trading_date_rollover(store, monkeypatch):
    """Yesterday's high-water mark must not gate today's session."""
    import hft_platform.execution.checkpoint as _ckpt

    monkeypatch.setattr(_ckpt, "_taifex_trading_date", lambda: "20260907")
    _build_drawdown(store)
    assert store.get_drawdown_pct() == pytest.approx(0.2)

    monkeypatch.setattr(_ckpt, "_taifex_trading_date", lambda: "20260908")

    assert store.get_drawdown_pct() == 0.0
    assert store._peak_equity_scaled == store.total_pnl


def test_peak_equity_holds_within_the_same_trading_date(store, monkeypatch):
    """The gate must not be weakened intraday -- only the rollover re-bases it."""
    import hft_platform.execution.checkpoint as _ckpt

    monkeypatch.setattr(_ckpt, "_taifex_trading_date", lambda: "20260907")
    _build_drawdown(store)

    for _ in range(5):
        assert store.get_drawdown_pct() == pytest.approx(0.2)
    assert store._peak_equity_scaled == 5_000_000


def test_drawdown_releases_at_the_rollover_while_flat_and_idle(store, monkeypatch):
    """The production stall: flat, no fills possible, drawdown its own input.

    StormGuard blocks every non-reducing intent above ``storm_drawdown_bps``.
    A flat strategy has nothing to reduce, so nothing it sends is permitted,
    and realised PnL -- the only input that could lower the drawdown -- is
    frozen by the block itself. Observed 2026-09-08: ``portfolio_drawdown_pct``
    held at one value to four decimal places for 8 h 29 min across 2,346
    rejected intents. The rollover
    is what has to break that cycle, without a fill and without a restart.
    """
    import hft_platform.execution.checkpoint as _ckpt

    monkeypatch.setattr(_ckpt, "_taifex_trading_date", lambda: "20260907")
    _build_drawdown(store)
    assert store.net_qty_for_symbol("SYM") == 0, "precondition: nothing left to reduce"
    assert store.get_drawdown_pct() > 0.0

    monkeypatch.setattr(_ckpt, "_taifex_trading_date", lambda: "20260908")

    assert store.get_drawdown_pct() == 0.0
    assert store.net_qty_for_symbol("SYM") == 0


def test_first_read_adopts_the_restored_watermark_without_rebasing(store, monkeypatch):
    """StartupReconciler restores the watermark *after* construction.

    The first read of a process must therefore adopt what recovery decided
    rather than treat the unset date as a rollover and discard it.
    """
    import hft_platform.execution.checkpoint as _ckpt

    monkeypatch.setattr(_ckpt, "_taifex_trading_date", lambda: "20260908")
    store._total_realized_pnl_scaled = 4_000_000
    store._peak_equity_scaled = 5_000_000

    assert store.get_drawdown_pct() == pytest.approx(0.2)
    assert store._peak_equity_scaled == 5_000_000


def test_rollover_uses_the_checkpoints_own_trading_date_definition(store, monkeypatch):
    """One boundary definition, not two.

    The checkpoint already scopes ``peak_equity_scaled`` with
    ``_taifex_trading_date``; the in-process re-base must read the same
    function, or the same engine answers differently depending only on whether
    it restarted.
    """
    import hft_platform.execution.checkpoint as _ckpt

    calls = []

    def _probe() -> str:
        calls.append(1)
        return "20260907"

    monkeypatch.setattr(_ckpt, "_taifex_trading_date", _probe)
    store.get_drawdown_pct()

    assert calls, "get_drawdown_pct did not consult the checkpoint trading date"


def test_fill_path_drawdown_does_not_consult_the_clock(store, monkeypatch):
    """The locked variant runs inside ``on_fill``; keep the date lookup off it."""
    import hft_platform.execution.checkpoint as _ckpt

    def _boom() -> str:
        raise AssertionError("trading-date lookup reached the fill path")

    monkeypatch.setattr(_ckpt, "_taifex_trading_date", _boom)
    _build_drawdown(store)
    assert store._get_drawdown_pct_locked() == pytest.approx(0.2)


class _RecordingGauge:
    """Records every value published to ``portfolio_drawdown_pct``."""

    def __init__(self) -> None:
        self.values: list[float] = []

    def set(self, value: float) -> None:
        self.values.append(value)


class _Metrics:
    def __init__(self) -> None:
        self.portfolio_drawdown_pct = _RecordingGauge()

    def cap_symbol(self, symbol: str) -> str:
        return symbol

    def __getattr__(self, name: str):  # pragma: no cover - inert stand-ins
        class _Any:
            def labels(self, **_kw):
                return self

            def set(self, *_a, **_k):
                return None

            def inc(self, *_a, **_k):
                return None

        return _Any()


def test_the_drawdown_read_by_the_risk_path_is_the_one_exported(store):
    """The gauge must carry the number the gate acts on, not a stale one.

    Until 2026-09-08 the two ``_on_fill`` sites were the gauge's only writers,
    so a drawdown that blocked every intent -- and therefore every fill --
    could never be published. On THESHOW the gauge read 0.0 while StormGuard
    gated on -111 bps.
    """
    metrics = _Metrics()
    store.metrics = metrics
    _build_drawdown(store)
    metrics.portfolio_drawdown_pct.values.clear()

    drawdown = store.get_drawdown_pct()

    assert drawdown == pytest.approx(0.2)
    assert metrics.portfolio_drawdown_pct.values, "get_drawdown_pct published nothing"
    assert metrics.portfolio_drawdown_pct.values[-1] == pytest.approx(drawdown)


def test_the_gauge_tracks_the_drawdown_without_any_new_fill(store):
    """The incident shape: no fill can occur, and the gauge must still update.

    A drawdown STORM blocks every non-reducing intent, so a flat strategy
    produces no fill. If the fill path is the only writer, the published
    drawdown is frozen exactly when it matters.
    """
    metrics = _Metrics()
    store.metrics = metrics
    _build_drawdown(store)
    metrics.portfolio_drawdown_pct.values.clear()

    for _ in range(3):
        store.get_drawdown_pct()

    assert len(metrics.portfolio_drawdown_pct.values) == 3
    assert all(v == pytest.approx(0.2) for v in metrics.portfolio_drawdown_pct.values)


def test_publishing_the_drawdown_never_breaks_the_risk_read(store):
    """A broken metrics backend must not take the risk path down with it."""

    class _Exploding:
        def set(self, _value: float) -> None:
            raise RuntimeError("metrics backend down")

    class _M:
        portfolio_drawdown_pct = _Exploding()

    _build_drawdown(store)
    store.metrics = _M()

    assert store.get_drawdown_pct() == pytest.approx(0.2)
