"""A restart must not turn realized PnL into drawdown.

Recovery restores ``_peak_equity_scaled`` and ``_total_realized_pnl_scaled``
from the checkpoint, but it does not reinstate every position the checkpoint
recorded, and it should not: ``load_recovery`` drops flat entries outright
(``net_qty == 0`` returns early), ``_recover_checkpoint_only`` filters them out
before that, and the broker-sourced paths replace the checkpoint's positions
with the broker's. ``PositionStore`` separately banks the realized PnL of
positions it removes itself -- the cardinality cap in ``_evict_flat_positions``
and ``clear_symbol_positions`` when reconciliation clears a phantom -- into
``_evicted_realized_pnl_scaled``, and that bank was never restored either.

The damage is delayed, which is why the restore looks correct. ``Position.update``
only moves ``realized_pnl_scaled`` when a position *closes*, so every *opening*
fill reaches ``_update_portfolio_aggregates`` with ``pnl_delta == 0`` and takes the
full-recompute branch: positions + recovery + bank. At that moment every
realized cent none of the three carries disappears from the total, while the
peak restored from the same checkpoint still contains it. The difference is
reported as drawdown that never happened, and ``get_drawdown_pct`` is what
StormGuard escalates on. A day that ends with most positions flat -- the normal
case -- restarts into a large false drawdown.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.execution import FillEvent, Side
from hft_platform.execution.checkpoint import PositionCheckpointWriter
from hft_platform.execution.positions import PositionStore
from hft_platform.execution.startup_recon import StartupPositionVerifier

# Realized PnL at unit-test scale sits far below the production drawdown floor.
_TEST_MIN_PEAK = 1_000


@pytest.fixture(autouse=True)
def _lower_drawdown_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    import hft_platform.execution.positions as _positions

    monkeypatch.setattr(_positions, "_MIN_PEAK_SCALED", _TEST_MIN_PEAK)


def _store() -> PositionStore:
    s = PositionStore()
    s.metrics = None  # no Prometheus side effects in unit tests
    return s


def _fill(symbol: str, side: Side, qty: int, price: int, fill_id: str) -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        account_id="ACC",
        order_id=f"O-{fill_id}",
        strategy_id="STRAT",
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=0,
        tax=0,
        ingest_ts_ns=0,
        match_ts_ns=0,
    )


def _round_trip(store: PositionStore, symbol: str, *, entry: int, exit_: int, qty: int = 1) -> None:
    """Open and close a position, leaving it flat with realized PnL."""
    store.on_fill(_fill(symbol, Side.BUY, qty, entry, f"{symbol}-open"))
    store.on_fill(_fill(symbol, Side.SELL, qty, exit_, f"{symbol}-close"))


def _recover_from(path: Path, trading_date: str) -> PositionStore:
    """A fresh store taken through recovery with the broker unavailable."""
    restarted = _store()
    client = MagicMock()
    client.get_positions.side_effect = RuntimeError("broker unavailable")
    verifier = StartupPositionVerifier(client, restarted, checkpoint_path=str(path))
    asyncio.run(verifier.recover(trading_date=trading_date, account_id="ACC"))
    return restarted


def _async_returning(value: dict[str, int]):
    """A stand-in for ``_fetch_broker_positions``."""

    async def _fetch() -> dict[str, int]:
        return value

    return _fetch


def _write_checkpoint(store: PositionStore, path: Path) -> str:
    """Write a checkpoint and return the trading date it recorded."""
    writer = PositionCheckpointWriter(store, path=str(path), interval_s=1)
    writer.write_checkpoint()
    return writer._trading_date_provider()


# ---------------------------------------------------------------------------
# The bank
# ---------------------------------------------------------------------------


def test_rebanking_absorbs_realized_pnl_no_restored_position_carries() -> None:
    store = _store()
    store.load_recovery(account_id="ACC", symbol="AAA", net_qty=1, avg_price_scaled=100, realized_pnl_scaled=300)

    banked = store.rebank_unaccounted_realized_pnl(1_000)

    assert banked == 700
    assert store._total_realized_pnl_scaled == 1_000


def test_rebanking_banks_nothing_when_the_positions_account_for_the_total() -> None:
    store = _store()
    store.load_recovery(account_id="ACC", symbol="AAA", net_qty=1, avg_price_scaled=100, realized_pnl_scaled=1_000)

    assert store.rebank_unaccounted_realized_pnl(1_000) == 0


def test_restore_portfolio_aggregates_reinstates_peak_and_total() -> None:
    store = _store()
    store._evicted_realized_pnl_scaled = 123_456  # stale value from a previous life

    store.restore_portfolio_aggregates(peak_equity_scaled=900, total_realized_pnl_scaled=800)

    assert store._peak_equity_scaled == 900
    assert store._total_realized_pnl_scaled == 800
    assert store._evicted_realized_pnl_scaled == 0


# ---------------------------------------------------------------------------
# The behaviour the bank exists for
# ---------------------------------------------------------------------------


def test_a_flat_position_does_not_become_drawdown_after_a_restart(tmp_path: Path) -> None:
    """The common case: a day's profit is in positions that ended flat.

    ``load_recovery`` drops them, so nothing in the restarted store carries
    their realized PnL -- but the restored peak does.
    """
    store = _store()
    _round_trip(store, "AAA", entry=1_000_000, exit_=1_100_000)  # flat, +profit
    store.on_fill(_fill("BBB", Side.BUY, 1, 1_000_000, "BBB-open"))  # still open
    peak = store._peak_equity_scaled
    assert store.get_drawdown_pct() == 0.0, "fixture is already in drawdown before the restart"

    path = tmp_path / "ckpt.json"
    restarted = _recover_from(path, _write_checkpoint(store, path))
    assert restarted._peak_equity_scaled == peak, "peak was not restored; test proves nothing"

    # An *opening* fill: the branch that recomputes the total from scratch.
    restarted.on_fill(_fill("CCC", Side.BUY, 1, 1_000_000, "CCC-open"))

    assert restarted.get_drawdown_pct() == 0.0, (
        f"restart booked drawdown that never happened: peak={restarted._peak_equity_scaled} "
        f"total={restarted._total_realized_pnl_scaled} banked={restarted._evicted_realized_pnl_scaled}"
    )


def test_an_evicted_position_does_not_become_drawdown_after_a_restart(tmp_path: Path) -> None:
    """The same defect reached through ``clear_symbol_positions``.

    Reconciliation clears a phantom position and banks its realized PnL; the
    checkpoint's ``positions`` no longer mention it at all.
    """
    store = _store()
    _round_trip(store, "AAA", entry=1_000_000, exit_=1_100_000)
    store.clear_symbol_positions("AAA")
    store.on_fill(_fill("BBB", Side.BUY, 1, 1_000_000, "BBB-open"))
    assert store._evicted_realized_pnl_scaled > 0, "fixture did not bank anything"
    peak = store._peak_equity_scaled

    path = tmp_path / "ckpt.json"
    restarted = _recover_from(path, _write_checkpoint(store, path))
    assert restarted._peak_equity_scaled == peak, "peak was not restored; test proves nothing"

    restarted.on_fill(_fill("CCC", Side.BUY, 1, 1_000_000, "CCC-open"))

    assert restarted.get_drawdown_pct() == 0.0, (
        f"restart booked drawdown that never happened: peak={restarted._peak_equity_scaled} "
        f"total={restarted._total_realized_pnl_scaled} banked={restarted._evicted_realized_pnl_scaled}"
    )


def test_a_real_loss_after_a_restart_is_still_reported_as_drawdown(tmp_path: Path) -> None:
    """The fix must not make the store blind to an actual drawdown."""
    store = _store()
    _round_trip(store, "AAA", entry=1_000_000, exit_=1_100_000)
    store.on_fill(_fill("BBB", Side.BUY, 1, 1_000_000, "BBB-open"))
    peak = store._peak_equity_scaled

    path = tmp_path / "ckpt.json"
    restarted = _recover_from(path, _write_checkpoint(store, path))

    # Close BBB at a loss large enough to erase the day's profit and then some.
    restarted.on_fill(_fill("BBB", Side.BUY, 1, 1_000_000, "BBB-reopen"))
    restarted.on_fill(_fill("BBB", Side.SELL, 1, 500_000, "BBB-loss"))

    assert restarted._total_realized_pnl_scaled < peak
    assert restarted.get_drawdown_pct() > 0.0


def test_a_halted_recovery_still_leaves_the_store_internally_consistent(tmp_path: Path) -> None:
    """A halt must not leave the aggregates describing positions that are absent.

    ``_recover_dual`` returns ``halted=True`` on a side mismatch *before* it
    writes anything to the store -- so the store holds a restored peak and total
    with no positions at all, the largest shortfall of any path.
    """
    store = _store()
    _round_trip(store, "AAA", entry=1_000_000, exit_=1_100_000)
    store.on_fill(_fill("BBB", Side.BUY, 1, 1_000_000, "BBB-open"))
    peak = store._peak_equity_scaled

    path = tmp_path / "ckpt.json"
    trading_date = _write_checkpoint(store, path)

    restarted = _store()
    verifier = StartupPositionVerifier(MagicMock(), restarted, checkpoint_path=str(path))
    # Broker reports the opposite side: a critical discrepancy, so recovery halts.
    verifier._fetch_broker_positions = _async_returning({"BBB": -1})  # type: ignore[method-assign]
    result = asyncio.run(verifier.recover(trading_date=trading_date, account_id="ACC"))
    assert result.halted, "fixture did not reach the halted branch"
    assert restarted._peak_equity_scaled == peak, "peak was not restored; test proves nothing"

    restarted.on_fill(_fill("CCC", Side.BUY, 1, 1_000_000, "CCC-open"))

    assert restarted.get_drawdown_pct() == 0.0, (
        f"halted restart booked drawdown that never happened: peak={restarted._peak_equity_scaled} "
        f"total={restarted._total_realized_pnl_scaled} banked={restarted._evicted_realized_pnl_scaled}"
    )


def test_merging_a_recovery_position_does_not_double_count_the_bank(tmp_path: Path) -> None:
    """The bank must not be re-added when a recovered position starts trading.

    ``_seed_from_recovery`` moves an entry out of ``_recovery_positions`` and
    into ``positions``. The bank was sized against the sum of *both*, so the
    move has to be PnL-neutral -- if it were not, the first fill on a recovered
    position would inflate or deflate the portfolio total.
    """
    store = _store()
    _round_trip(store, "AAA", entry=1_000_000, exit_=1_100_000)  # flat, dropped on restore
    store.on_fill(_fill("BBB", Side.BUY, 1, 1_000_000, "BBB-open"))  # open, restored
    total_before = store._total_realized_pnl_scaled

    path = tmp_path / "ckpt.json"
    restarted = _recover_from(path, _write_checkpoint(store, path))
    assert restarted._total_realized_pnl_scaled == total_before

    # Trade the recovered position: this is the seed-from-recovery merge.
    restarted.on_fill(_fill("BBB", Side.SELL, 1, 1_000_000, "BBB-flat"))
    # ...and then an opening fill, which forces the full recompute.
    restarted.on_fill(_fill("CCC", Side.BUY, 1, 1_000_000, "CCC-open"))

    assert restarted._total_realized_pnl_scaled == total_before, (
        f"the merge changed the portfolio total: {restarted._total_realized_pnl_scaled} != {total_before}"
    )
    assert restarted.get_drawdown_pct() == 0.0


# ---------------------------------------------------------------------------
# The two doors the first version of this fix left open (Codex review, 2026-08-27)
# ---------------------------------------------------------------------------


def test_clearing_a_recovery_position_banks_its_realized_pnl() -> None:
    """``clear_symbol_positions`` banked live positions but plain-deleted recovery ones.

    The asymmetry was invisible until rebanking made recovery entries load-bearing:
    ``rebank_unaccounted_realized_pnl`` counts their realized PnL as *accounted*, so a
    later reconciliation clear removed PnL that the bank had already declined to cover.
    """
    store = _store()
    store.load_recovery(account_id="ACC", symbol="AAA", net_qty=1, avg_price_scaled=100, realized_pnl_scaled=500)

    store.clear_symbol_positions("AAA")

    assert store._evicted_realized_pnl_scaled == 500


def test_a_reconciliation_clear_does_not_resurface_as_drawdown() -> None:
    """Restore, clear a phantom, then trade: the total must survive all three."""
    store = _store()
    store.restore_portfolio_aggregates(peak_equity_scaled=100_000, total_realized_pnl_scaled=100_000)
    store.load_recovery(account_id="ACC", symbol="AAA", net_qty=1, avg_price_scaled=100, realized_pnl_scaled=100_000)

    store.clear_symbol_positions("AAA")
    # An *opening* fill carries pnl_delta == 0 and so takes the full-recompute branch.
    store.on_fill(_fill("BBB", Side.BUY, 1, 100, "bbb-open"))

    assert store._total_realized_pnl_scaled == 100_000
    assert store.get_drawdown_pct() == 0.0


def test_clearing_a_live_and_a_recovery_position_banks_both() -> None:
    store = _store()
    _round_trip(store, "AAA", entry=100, exit_=200)  # a live position carrying realized PnL
    store.load_recovery(account_id="ACC", symbol="AAA", net_qty=1, avg_price_scaled=100, realized_pnl_scaled=400)
    live_pnl = sum(p.realized_pnl_scaled for p in store.positions.values() if p.symbol == "AAA")

    store.clear_symbol_positions("AAA")

    assert store._evicted_realized_pnl_scaled == live_pnl + 400


def test_a_checkpoint_recording_zero_totals_is_still_restored(tmp_path: Path) -> None:
    """peak=0 and total=0 is a valid record, not an absent one.

    A retained position at +100000 against an evicted -100000 totals exactly zero.
    Reading that zero as "this checkpoint has no aggregates" skipped the rebank, and
    the next opening fill recomputed the total as the retained +100000 alone.
    """
    source = _store()
    source.load_recovery(account_id="ACC", symbol="AAA", net_qty=1, avg_price_scaled=100, realized_pnl_scaled=100_000)
    source._evicted_realized_pnl_scaled = -100_000
    source._total_realized_pnl_scaled = 0
    source._peak_equity_scaled = 0
    path = tmp_path / "ckpt.json"
    trading_date = _write_checkpoint(source, path)

    restarted = _recover_from(path, trading_date)
    restarted.on_fill(_fill("BBB", Side.BUY, 1, 100, "bbb-open"))

    assert restarted._total_realized_pnl_scaled == 0
    assert restarted._peak_equity_scaled == 0
    assert restarted.get_drawdown_pct() == 0.0


def test_a_checkpoint_with_no_aggregate_fields_at_all_is_left_alone(tmp_path: Path) -> None:
    """A pre-M2 checkpoint has genuinely absent fields; it must not be rebanked to zero."""
    source = _store()
    _round_trip(source, "AAA", entry=100, exit_=200)
    path = tmp_path / "ckpt.json"
    trading_date = _write_checkpoint(source, path)

    import json

    payload = json.loads(path.read_text())
    payload.pop("peak_equity_scaled", None)
    payload.pop("total_realized_pnl_scaled", None)
    path.write_text(json.dumps(payload))

    restarted = _recover_from(path, trading_date)

    # Nothing was restored, so nothing was rebanked -- the bank stays untouched
    # rather than being forced to balance against a total that was never read.
    assert restarted._evicted_realized_pnl_scaled == 0
