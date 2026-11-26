"""Tests for scripts/latency/fubon_rtt_bulk_probe.py: stats, URL refusal, fill abort, leftover cancel.

Nothing here connects to a broker: the SDK is the autospec'd stand-in from fubon_mock_helper
(or a scripted fake facade), so a wrong call fails the test.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from tests.unit import fubon_mock_helper
from tests.unit.fubon_mock_helper import SdkFactory, fail, install_fubon_neo_mock, make_account, make_order_result, ok

install_fubon_neo_mock()

from hft_platform.feed_adapter.fubon import facade as facade_module
from hft_platform.feed_adapter.fubon.facade import FubonClientFacade
from hft_platform.feed_adapter.fubon.order_gateway import FubonCancelNoOp

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "latency"
_OFFICIAL_URL = "wss://neoapitest.fbs.com.tw/TASP/XCPXWS"
_SENTINEL = "sentinel-not-a-real-credential"

_ENV_NAMES = (
    "HFT_ORDER_MODE",
    "HFT_FUBON_TEST_URL",
    "HFT_FUBON_PERSONAL_ID",
    "HFT_FUBON_API_KEY",
    "HFT_FUBON_PASSWORD",
    "HFT_FUBON_CERT_PATH",
    "HFT_FUBON_CERT_PASS",
    "HFT_FUBON_ACCOUNT",
)


def _load_script(name: str) -> Any:
    path = _SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_script("fubon_rtt_bulk_probe")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #


class TestStats:
    def test_summarize_matches_numpy_percentiles_for_known_samples(self) -> None:
        samples = list(range(1, 101))

        stats = probe._summarize("place_order", samples, errors=2)

        arr = np.asarray(samples, dtype=np.float64)
        assert stats.op == "place_order"
        assert stats.count == 100
        assert stats.errors == 2
        assert stats.mean_us == pytest.approx(50.5)
        assert stats.std_us == pytest.approx(float(arr.std(ddof=0)))
        assert stats.p50_us == pytest.approx(50.5)
        assert stats.p90_us == pytest.approx(float(np.percentile(arr, 90)))
        assert stats.p95_us == pytest.approx(float(np.percentile(arr, 95)))
        assert stats.p99_us == pytest.approx(float(np.percentile(arr, 99)))
        assert stats.max_us == 100.0

    def test_summarize_bootstrap_ci_brackets_the_point_estimate(self) -> None:
        stats = probe._summarize("cancel_order", list(range(1, 201)), errors=0)

        assert stats.p50_ci95_us[0] <= stats.p50_us <= stats.p50_ci95_us[1]
        assert stats.p95_ci95_us[0] <= stats.p95_us <= stats.p95_ci95_us[1]
        assert stats.p95_ci95_us[0] < stats.p95_ci95_us[1]

    def test_summarize_is_deterministic_across_calls(self) -> None:
        samples = [(i * 37) % 997 + 1 for i in range(120)]

        assert probe._summarize("op", samples, 0) == probe._summarize("op", samples, 0)

    def test_summarize_with_no_samples_returns_zeroed_stats_and_keeps_the_error_count(self) -> None:
        stats = probe._summarize("submitted_ack", [], errors=7)

        assert stats.count == 0
        assert stats.errors == 7
        assert (stats.mean_us, stats.p50_us, stats.p95_us, stats.max_us) == (0.0, 0.0, 0.0, 0.0)
        assert stats.p50_ci95_us == (0.0, 0.0)
        assert stats.p95_ci95_us == (0.0, 0.0)

    def test_summarize_with_fewer_than_ten_samples_gives_a_zero_width_ci(self) -> None:
        stats = probe._summarize("op", [5, 6, 7], errors=0)

        assert stats.count == 3
        assert stats.p50_ci95_us == (0.0, 0.0)
        assert stats.p95_ci95_us == (0.0, 0.0)

    def test_op_stats_fields_match_the_shioaji_probe_schema(self) -> None:
        tree = ast.parse((_SCRIPTS / "shioaji_rtt_bulk_probe.py").read_text())
        shioaji_fields = [
            node.target.id
            for cls in ast.walk(tree)
            if isinstance(cls, ast.ClassDef) and cls.name == "OpStats"
            for node in cls.body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
        ]

        assert shioaji_fields, "OpStats not found in the shioaji probe"
        assert [f.name for f in dataclasses.fields(probe.OpStats)] == shioaji_fields


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #


class TestPricing:
    def test_price_for_side_puts_buy_below_and_sell_above_the_reference(self) -> None:
        reference = 220_000_000  # 22000 points, scaled x10000

        assert probe.price_for_side("Buy", reference, 300, 10_000) == 217_000_000
        assert probe.price_for_side("Sell", reference, 300, 10_000) == 223_000_000

    def test_price_for_side_never_prices_a_buy_below_one_tick(self) -> None:
        assert probe.price_for_side("Buy", 1_000_000, 300, 10_000) == 10_000

    def test_points_to_scaled_is_integer_exact_for_quote_prices(self) -> None:
        assert probe._points_to_scaled(22000.0) == 220_000_000
        assert probe._points_to_scaled(22000.5) == 220_005_000
        assert probe._points_to_scaled(22000) == 220_000_000

    @pytest.mark.parametrize("bad", [None, True, 0, -5.0, float("nan"), float("inf"), "22000"])
    def test_points_to_scaled_rejects_anything_that_is_not_a_positive_finite_number(self, bad: Any) -> None:
        assert probe._points_to_scaled(bad) is None

    def test_quote_source_refreshes_every_n_calls_and_keeps_the_previous_reference_on_failure(self) -> None:
        sdk = MagicMock()
        quote = sdk.marketdata.rest_client.futopt.intraday.quote
        quote.side_effect = [
            {"lastTrade": {"price": 22000.0}},
            {"lastTrade": {"price": 23000.0}},
            RuntimeError("quote service down"),
        ]
        source = probe.QuotePriceSource(sdk, "TXFL6", offset_ticks=300, tick_scaled=10_000, refresh_every=2)

        prices = [source("Buy") for _ in range(5)]

        assert prices == [217_000_000, 217_000_000, 227_000_000, 227_000_000, 227_000_000]
        assert quote.call_count == 3

    def test_quote_source_answers_none_when_no_reference_was_ever_obtained(self) -> None:
        sdk = MagicMock()
        sdk.marketdata.rest_client.futopt.intraday.quote.return_value = {"lastTrade": {"price": None}}
        source = probe.QuotePriceSource(sdk, "TXFL6", offset_ticks=300, tick_scaled=10_000)

        assert source("Sell") is None

    def test_quote_source_initialises_realtime_once_when_the_sdk_has_no_marketdata(self) -> None:
        class _Sdk:
            def __init__(self) -> None:
                self.marketdata: Any = None
                self.init_calls = 0

            def init_realtime(self) -> None:
                self.init_calls += 1
                self.marketdata = MagicMock()
                self.marketdata.rest_client.futopt.intraday.quote.return_value = {"lastTrade": {"price": 22000.0}}

        sdk = _Sdk()
        source = probe.QuotePriceSource(sdk, "TXFL6", offset_ticks=300, tick_scaled=10_000)

        assert source("Buy") == 217_000_000
        assert source("Sell") == 223_000_000
        assert sdk.init_calls == 1


# --------------------------------------------------------------------------- #
# URL / environment refusal
# --------------------------------------------------------------------------- #


class TestTargetGuard:
    def test_validate_probe_target_accepts_the_official_test_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_TEST_URL", _OFFICIAL_URL)

        assert probe.validate_probe_target() == _OFFICIAL_URL

    @pytest.mark.parametrize(
        ("url", "mode", "fragment"),
        [
            ("wss://neoapi.fbs.com.tw/TASP/XCPXWS", None, "production trading host"),
            (None, None, "is not set"),
            ("https://neoapitest.fbs.com.tw/x", None, "ws:// or wss://"),
            ("ws://neoapitest.fbs.com.tw/x", None, "must be wss://neoapitest.fbs.com.tw"),
            ("wss://fubon-test.example.invalid/ws", None, "must be wss://neoapitest.fbs.com.tw"),
            ("wss://neoapitest.fbs.com.tw.evil.example/x", None, "must be wss://neoapitest.fbs.com.tw"),
            (_OFFICIAL_URL, "live", "is live"),
            (_OFFICIAL_URL, "real", "is live"),
        ],
    )
    def test_validate_probe_target_refuses_an_unsafe_environment(
        self, monkeypatch: pytest.MonkeyPatch, url: str | None, mode: str | None, fragment: str
    ) -> None:
        if url is not None:
            monkeypatch.setenv("HFT_FUBON_TEST_URL", url)
        if mode is not None:
            monkeypatch.setenv("HFT_ORDER_MODE", mode)

        with pytest.raises(probe.ProbeRefusedError, match=fragment):
            probe.validate_probe_target()

    def test_main_refuses_a_production_url_before_any_sdk_is_built(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        factory = SdkFactory()
        monkeypatch.setattr(facade_module, "FubonSDK", factory)
        monkeypatch.setenv("HFT_FUBON_TEST_URL", "wss://neoapi.fbs.com.tw/TASP/XCPXWS")
        out = tmp_path / "report.json"

        rc = probe.main(["--symbol", "TXFL6", "--out", str(out)])

        assert rc == 2
        assert factory.calls == []
        assert not out.exists()

    def test_main_refuses_live_order_mode_before_any_sdk_is_built(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        factory = SdkFactory()
        monkeypatch.setattr(facade_module, "FubonSDK", factory)
        monkeypatch.setenv("HFT_FUBON_TEST_URL", _OFFICIAL_URL)
        monkeypatch.setenv("HFT_ORDER_MODE", "live")

        rc = probe.main(["--symbol", "TXFL6", "--out", str(tmp_path / "report.json")])

        assert rc == 2
        assert factory.calls == []

    def test_main_refuses_when_the_sdk_is_not_installed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(facade_module, "FubonSDK", None)
        monkeypatch.setenv("HFT_FUBON_TEST_URL", _OFFICIAL_URL)

        rc = probe.main(["--symbol", "TXFL6", "--out", str(tmp_path / "report.json")])

        assert rc == 2

    @pytest.mark.parametrize(
        "argv",
        [
            ["--out", "x.json"],
            ["--symbol", "TXFL6", "--offset-ticks", "0", "--out", "x.json"],
            ["--symbol", "TXFL6", "--iters", "0", "--out", "x.json"],
            ["--symbol", "TXFL6", "--price", "217000000", "--out", "x.json"],
            ["--symbol", "TXFL6", "--side", "Buy", "--price", "-1", "--out", "x.json"],
        ],
    )
    def test_main_refuses_unsafe_arguments(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, argv: list[str]
    ) -> None:
        factory = SdkFactory()
        monkeypatch.setattr(facade_module, "FubonSDK", factory)
        monkeypatch.setenv("HFT_FUBON_TEST_URL", _OFFICIAL_URL)
        monkeypatch.chdir(tmp_path)

        rc = probe.main(argv)

        assert rc == 2
        assert factory.calls == []

    def test_main_builds_the_sdk_against_the_test_url_only_and_reports_a_test_mode_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        helper = fubon_mock_helper
        factory = probe._DryRunSdkFactory(helper, "TXFL6")
        monkeypatch.setattr(facade_module, "FubonSDK", factory)
        monkeypatch.setenv("HFT_FUBON_TEST_URL", _OFFICIAL_URL)
        monkeypatch.setenv("HFT_FUBON_PERSONAL_ID", "A123456789")
        monkeypatch.setenv("HFT_FUBON_PASSWORD", "pwd")
        monkeypatch.setenv("HFT_FUBON_CERT_PATH", "/c.pfx")
        out = tmp_path / "report.json"

        rc = probe.main(
            ["--symbol", "TXFL6", "--side", "Buy", "--price", "217000000", "--iters", "3", "--warmup", "0",
             "--sleep", "0", "--out", str(out)]
        )  # fmt: skip

        report = json.loads(out.read_text())
        assert rc == 0
        assert factory.calls == [((), {"url": _OFFICIAL_URL})]
        assert report["meta"]["mode"] == "test"
        assert report["meta"]["synthetic"] is False
        assert report["meta"]["explicit_price_scaled"] == 217_000_000
        assert report["place_order"]["count"] == 3


# --------------------------------------------------------------------------- #
# Probe core with a scripted fake facade
# --------------------------------------------------------------------------- #


class _FakeFacade:
    """Scriptable stand-in for FubonClientFacade: acks through the registered callbacks."""

    def __init__(self) -> None:
        self.on_order: Any = None
        self.on_deal: Any = None
        self.place_kwargs: list[dict[str, Any]] = []
        self.placed: list[SimpleNamespace] = []
        self.cancel_calls: list[str] = []
        self.cancelled: list[str] = []
        self.cancel_script: list[str] = []
        self.place_error = False
        self.deliver_ack = True
        self.ack_filled_qty = 0
        self.fill_after_place = False
        self.place_filled_lot = 0

    def set_execution_callbacks(self, on_order: Any, on_deal: Any) -> None:
        self.on_order = on_order
        self.on_deal = on_deal

    def place_order(self, **kwargs: Any) -> SimpleNamespace:
        self.place_kwargs.append(kwargs)
        if self.place_error:
            raise RuntimeError("place rejected")
        n = len(self.placed) + 1
        order = SimpleNamespace(order_no=f"o{n}", seq_no=f"s{n}", filled_lot=self.place_filled_lot)
        self.placed.append(order)
        if self.deliver_ack:
            self.on_order(
                "order",
                {"order_id": order.order_no, "seq_no": order.seq_no, "order": {"filled_qty": self.ack_filled_qty}},
            )
        if self.fill_after_place:
            self.on_deal({"order_id": order.order_no, "qty": 1})
        return order

    def cancel_order(self, order: Any) -> Any:
        self.cancel_calls.append(order.order_no)
        action = self.cancel_script.pop(0) if self.cancel_script else "ok"
        if action == "raise":
            raise RuntimeError("cancel timeout")
        if action == "noop":
            return FubonCancelNoOp(order_id=order.order_no, reason="already_filled")
        self.cancelled.append(order.order_no)
        return SimpleNamespace(order_no=order.order_no)


def _make_probe(
    facade: _FakeFacade,
    price_source: Any = None,
    **overrides: Any,
) -> Any:
    fields: dict[str, Any] = {
        "symbol": "TXFL6",
        "side": "Buy",
        "offset_ticks": 300,
        "tick_scaled": 10_000,
        "explicit_price": None,
        "market_type": "Future",
        "iters": 4,
        "warmup": 0,
        "sleep_s": 0.0,
        "ack_timeout_s": 0.05,
    }
    fields.update(overrides)
    tracker = probe.ExecTracker()
    facade.set_execution_callbacks(tracker.on_order, tracker.on_deal)
    source = price_source or probe.FixedPriceSource(217_000_000)
    return probe.RttProbe(facade, probe.ProbeConfig(**fields), source, tracker, sleep=lambda _s: None)


class TestCleanRun:
    def test_run_records_one_sample_per_operation_per_iteration(self) -> None:
        facade = _FakeFacade()
        runner = _make_probe(facade, iters=3)

        runner.run()

        assert len(runner.place_samples) == 3
        assert len(runner.submitted_ack_samples) == 3
        assert len(runner.cancel_samples) == 3
        assert (runner.place_errors, runner.submitted_ack_errors, runner.cancel_errors) == (0, 0, 0)
        assert runner.abort_reason is None
        assert facade.cancelled == ["o1", "o2", "o3"]
        assert runner.open_orders == ()
        assert probe._exit_code(runner) == 0

    def test_orders_are_limit_rod_one_lot_with_alternating_sides_and_the_source_price(self) -> None:
        facade = _FakeFacade()
        runner = _make_probe(facade, side="alternate", iters=4, market_type="FutureNight")

        runner.run()

        assert [kw["side"] for kw in facade.place_kwargs] == ["Buy", "Sell", "Buy", "Sell"]
        for kw in facade.place_kwargs:
            assert kw["price"] == 217_000_000
            assert kw["qty"] == 1
            assert kw["tif"] == "ROD"
            assert kw["price_type"] == "LMT"
            assert kw["market_type"] == "FutureNight"
            assert kw["symbol"] == "TXFL6"
            assert "contract_code" not in kw

    def test_warmup_cycles_are_placed_and_cancelled_but_not_recorded(self) -> None:
        facade = _FakeFacade()
        runner = _make_probe(facade, iters=2, warmup=3)

        runner.run()

        assert len(facade.placed) == 5
        assert len(runner.place_samples) == 2
        assert runner.iters_recorded == 2
        assert facade.cancelled == ["o1", "o2", "o3", "o4", "o5"]

    def test_ack_arriving_before_place_order_returns_is_still_measured(self) -> None:
        facade = _FakeFacade()  # acks inside place_order, i.e. before it returns
        runner = _make_probe(facade, iters=2)

        runner.run()

        assert len(runner.submitted_ack_samples) == 2
        assert all(sample >= 0 for sample in runner.submitted_ack_samples)

    def test_missing_ack_counts_an_error_but_the_order_is_still_cancelled(self) -> None:
        facade = _FakeFacade()
        facade.deliver_ack = False
        runner = _make_probe(facade, iters=2, ack_timeout_s=0.01)  # 10 ms: the timeout under test

        runner.run()

        assert runner.submitted_ack_samples == []
        assert runner.submitted_ack_errors == 2
        assert len(runner.cancel_samples) == 2
        assert facade.cancelled == ["o1", "o2"]


class TestFailureBreaker:
    def test_five_consecutive_place_failures_abort_the_probe(self) -> None:
        facade = _FakeFacade()
        facade.place_error = True
        runner = _make_probe(facade, iters=20)

        runner.run()

        assert len(facade.place_kwargs) == 5
        assert runner.abort_reason == "consecutive_place_errors"
        assert runner.place_errors == 5
        assert probe._exit_code(runner) == 3

    def test_a_price_source_without_a_reference_places_nothing_and_aborts(self) -> None:
        facade = _FakeFacade()
        runner = _make_probe(facade, price_source=lambda side: None, iters=20)

        runner.run()

        assert facade.place_kwargs == []
        assert runner.abort_reason == "consecutive_place_errors"


class TestFillAbort:
    def test_deal_callback_after_place_aborts_and_skips_the_remaining_iterations(self) -> None:
        facade = _FakeFacade()
        facade.fill_after_place = True
        runner = _make_probe(facade, iters=5)

        runner.run()

        assert len(facade.placed) == 1
        assert runner.fills_aborted == 1
        assert runner.abort_reason == "fill"
        assert runner.place_samples == []
        assert runner.cancel_samples == []
        assert probe._exit_code(runner) == 3

    def test_filled_lot_on_the_place_result_aborts(self) -> None:
        facade = _FakeFacade()
        facade.place_filled_lot = 1
        runner = _make_probe(facade, iters=5)

        runner.run()

        assert len(facade.placed) == 1
        assert runner.abort_reason == "fill"
        assert facade.cancel_calls == ["o1"]  # not cancelled in the cycle: only by the exit sweep

    def test_order_callback_reporting_a_filled_quantity_aborts(self) -> None:
        facade = _FakeFacade()
        facade.ack_filled_qty = 1
        runner = _make_probe(facade, iters=5)

        runner.run()

        assert len(facade.placed) == 1
        assert runner.abort_reason == "fill"

    def test_cancel_refused_as_already_filled_aborts_the_probe(self) -> None:
        facade = _FakeFacade()
        facade.cancel_script = ["noop"]
        runner = _make_probe(facade, iters=5)

        runner.run()

        assert len(facade.placed) == 1
        assert runner.fills_aborted == 1
        assert runner.abort_reason == "fill"
        assert runner.cancel_samples == []
        assert runner.open_orders == ()

    def test_real_gateway_already_filled_cancel_text_aborts_the_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name, value in (
            ("HFT_FUBON_TEST_URL", _OFFICIAL_URL),
            ("HFT_FUBON_PERSONAL_ID", "A123456789"),
            ("HFT_FUBON_PASSWORD", "pwd"),
            ("HFT_FUBON_CERT_PATH", "/c.pfx"),
        ):
            monkeypatch.setenv(name, value)

        class _Factory(SdkFactory):
            def __call__(self, *args: Any, **kwargs: Any) -> MagicMock:
                sdk = super().__call__(*args, **kwargs)
                sdk.login.return_value = ok([make_account()])
                sdk.futopt.place_order.return_value = ok(make_order_result(order_no="l0001", filled_lot=0))
                sdk.futopt.cancel_order.return_value = fail("成交單已不允許取消交易")
                return sdk

        factory = _Factory()
        monkeypatch.setattr(facade_module, "FubonSDK", factory)
        facade = FubonClientFacade(symbols_path=None, broker_config={"broker": "fubon"})
        tracker = probe.ExecTracker()
        probe._connect(facade, tracker)
        config = probe.ProbeConfig(
            symbol="TXFL6", side="Buy", offset_ticks=300, tick_scaled=10_000, explicit_price=217_000_000,
            market_type="Future", iters=5, warmup=0, sleep_s=0.0, ack_timeout_s=0.01,
        )  # fmt: skip
        runner = probe.RttProbe(facade, config, probe.FixedPriceSource(217_000_000), tracker, sleep=lambda _s: None)

        runner.run()

        sdk = factory.instances[0]
        assert sdk.futopt.place_order.call_count == 1
        assert runner.abort_reason == "fill"
        assert runner.fills_aborted == 1


class TestLeftoverSweep:
    def test_failed_in_cycle_cancels_are_retried_by_the_exit_sweep(self) -> None:
        facade = _FakeFacade()
        facade.cancel_script = ["raise", "raise", "raise"]
        runner = _make_probe(facade, iters=3)

        runner.run()

        assert runner.cancel_samples == []
        assert runner.cancel_errors == 3
        assert runner.leftovers_cancelled == 3
        assert runner.leftovers_failed == 0
        assert facade.cancelled == ["o1", "o2", "o3"]
        assert runner.open_orders == ()
        assert probe._exit_code(runner) == 0

    def test_exit_sweep_runs_when_an_exception_escapes_the_loop(self) -> None:
        facade = _FakeFacade()
        facade.cancel_script = ["raise", "raise"]
        calls = {"n": 0}

        def price_source(side: str) -> int:
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("quote source exploded")
            return 217_000_000

        runner = _make_probe(facade, price_source=price_source, iters=10)

        with pytest.raises(RuntimeError, match="quote source exploded"):
            runner.run()

        assert facade.cancelled == ["o1", "o2"]
        assert runner.open_orders == ()

    def test_exit_sweep_runs_on_keyboard_interrupt(self) -> None:
        facade = _FakeFacade()
        facade.cancel_script = ["raise"]
        calls = {"n": 0}

        def price_source(side: str) -> int:
            calls["n"] += 1
            if calls["n"] == 2:
                raise KeyboardInterrupt
            return 217_000_000

        runner = _make_probe(facade, price_source=price_source, iters=10)

        with pytest.raises(KeyboardInterrupt):
            runner.run()

        assert facade.cancelled == ["o1"]
        assert runner.open_orders == ()

    def test_sweep_reports_orders_it_could_not_cancel(self) -> None:
        facade = _FakeFacade()
        facade.cancel_script = ["raise"] * 6
        runner = _make_probe(facade, iters=3)

        runner.run()

        assert runner.leftovers_failed == 3
        assert runner.leftover_failed_ids == ["o1", "o2", "o3"]
        assert [order.order_no for order in runner.open_orders] == ["o1", "o2", "o3"]
        assert probe._exit_code(runner) == 4

    def test_sweep_counts_a_leftover_that_turns_out_to_be_filled(self) -> None:
        facade = _FakeFacade()
        facade.cancel_script = ["raise", "noop"]
        runner = _make_probe(facade, iters=1)

        runner.run()

        assert runner.leftovers_filled == 1
        assert runner.leftovers_cancelled == 0
        assert runner.abort_reason == "fill"
        assert runner.fills_aborted == 1
        assert runner.open_orders == ()

    def test_sweep_cancels_nothing_when_every_order_was_already_cancelled(self) -> None:
        facade = _FakeFacade()
        runner = _make_probe(facade, iters=2)

        runner.run()

        assert facade.cancel_calls == ["o1", "o2"]  # one cancel per order, none repeated by the sweep
        assert runner.leftovers_cancelled == 0

    def test_exit_code_ranks_leftover_orders_above_an_abort(self) -> None:
        both = SimpleNamespace(leftovers_failed=1, abort_reason="fill")
        abort_only = SimpleNamespace(leftovers_failed=0, abort_reason="fill")
        clean = SimpleNamespace(leftovers_failed=0, abort_reason=None)

        assert (probe._exit_code(both), probe._exit_code(abort_only), probe._exit_code(clean)) == (4, 3, 0)


# --------------------------------------------------------------------------- #
# Execution tracker
# --------------------------------------------------------------------------- #


class TestExecTracker:
    def test_order_changed_callback_is_not_counted_as_the_submit_ack(self) -> None:
        tracker = probe.ExecTracker()

        tracker.on_order("order_changed", {"order_id": "o1", "seq_no": "s1", "order": {"filled_qty": 0}})

        assert tracker.wait_ack(("o1", "s1"), 0) is None

    def test_first_order_callback_wins_over_later_ones_for_the_same_order(self) -> None:
        tracker = probe.ExecTracker()
        tracker.on_order("order", {"order_id": "o1", "order": {}})
        first = tracker.wait_ack(("o1",), 0)

        tracker.on_order("order", {"order_id": "o1", "order": {}})

        assert first is not None
        assert tracker.wait_ack(("o1",), 0) == first

    def test_ack_is_found_by_sequence_number_when_the_order_number_differs(self) -> None:
        tracker = probe.ExecTracker()

        tracker.on_order("order", {"order_id": "o9", "seq_no": "s1", "order": {}})

        assert tracker.wait_ack(("o1", "s1"), 0) is not None

    def test_ack_map_evicts_the_oldest_entry_beyond_its_capacity(self) -> None:
        tracker = probe.ExecTracker(capacity=2)

        for key in ("a", "b", "c"):
            tracker.on_order("order", {"order_id": key, "order": {}})

        assert tracker.wait_ack(("a",), 0) is None
        assert tracker.wait_ack(("b",), 0) is not None
        assert tracker.wait_ack(("c",), 0) is not None

    def test_deal_callback_marks_a_fill_and_wakes_a_waiting_ack(self) -> None:
        tracker = probe.ExecTracker()

        tracker.on_deal({"order_id": "o1"})

        assert tracker.fill_seen is True
        assert tracker.wait_ack(("o1",), 5.0) is None  # returns at once because a fill was seen

    def test_non_dict_payload_is_ignored_without_raising(self) -> None:
        tracker = probe.ExecTracker()

        tracker.on_order("order", None)
        tracker.on_order("order", "garbage")

        assert tracker.fill_seen is False
        assert tracker.wait_ack(("o1",), 0) is None

    def test_wait_ack_with_no_keys_returns_none_without_waiting(self) -> None:
        assert probe.ExecTracker().wait_ack((), 5.0) is None


# --------------------------------------------------------------------------- #
# Dry run (mocked SDK, synthetic latencies)
# --------------------------------------------------------------------------- #

_SHIOAJI_TOP_KEYS = ("meta", "place_order", "submitted_ack", "cancel_order", "raw")
_SHIOAJI_RAW_KEYS = ("place_samples_us", "submitted_ack_samples_us", "cancel_samples_us")


class TestDryRun:
    def _run(self, tmp_path: Path, *extra: str) -> tuple[int, dict[str, Any]]:
        out = tmp_path / "dry.json"
        rc = probe.main(["--dry-run", "--iters", "6", "--warmup", "1", "--sleep", "0", "--out", str(out), *extra])
        return rc, json.loads(out.read_text())

    def test_dry_run_writes_a_synthetic_report_in_the_shioaji_schema(self, tmp_path: Path) -> None:
        rc, report = self._run(tmp_path)

        shioaji_strings = {
            node.value
            for node in ast.walk(ast.parse((_SCRIPTS / "shioaji_rtt_bulk_probe.py").read_text()))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert rc == 0
        assert set(_SHIOAJI_TOP_KEYS + _SHIOAJI_RAW_KEYS) <= shioaji_strings
        assert tuple(report) == _SHIOAJI_TOP_KEYS
        assert tuple(report["raw"]) == _SHIOAJI_RAW_KEYS
        op_fields = [f.name for f in dataclasses.fields(probe.OpStats)]
        for op in ("place_order", "submitted_ack", "cancel_order"):
            assert list(report[op]) == op_fields
            assert report[op]["count"] == 6
            assert report[op]["errors"] == 0
        assert report["meta"]["mode"] == "dry-run"
        assert report["meta"]["synthetic"] is True
        assert report["meta"]["abort_reason"] is None
        assert report["meta"]["leftovers_failed"] == 0
        assert report["meta"]["iters_recorded"] == 6
        assert report["meta"]["ts_utc"].endswith("Z")

    def test_dry_run_raw_samples_are_positive_integer_microseconds_matching_the_counts(self, tmp_path: Path) -> None:
        _rc, report = self._run(tmp_path)

        for key, op in zip(_SHIOAJI_RAW_KEYS, ("place_order", "submitted_ack", "cancel_order"), strict=True):
            samples = report["raw"][key]
            assert len(samples) == report[op]["count"] == 6
            assert all(isinstance(s, int) and s >= 0 for s in samples)
        assert all(s > 0 for s in report["raw"]["place_samples_us"])
        assert report["place_order"]["max_us"] == max(report["raw"]["place_samples_us"])

    def test_dry_run_with_explicit_price_and_sell_side_works_offline(self, tmp_path: Path) -> None:
        rc, report = self._run(tmp_path, "--side", "Sell", "--price", "230000000")

        assert rc == 0
        assert report["meta"]["side"] == "Sell"
        assert report["meta"]["explicit_price_scaled"] == 230_000_000

    def test_dry_run_restores_the_environment_and_the_sdk_class(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HFT_ORDER_MODE", "live")
        monkeypatch.setenv("HFT_FUBON_PERSONAL_ID", _SENTINEL)
        monkeypatch.setenv("HFT_FUBON_API_KEY", _SENTINEL)
        sdk_before = facade_module.FubonSDK

        rc, report = self._run(tmp_path)

        assert rc == 0
        assert facade_module.FubonSDK is sdk_before
        assert probe.os.environ["HFT_ORDER_MODE"] == "live"
        assert probe.os.environ["HFT_FUBON_PERSONAL_ID"] == _SENTINEL
        assert probe.os.environ["HFT_FUBON_API_KEY"] == _SENTINEL
        assert "HFT_FUBON_TEST_URL" not in probe.os.environ
        assert _SENTINEL not in json.dumps(report)

    def test_dry_run_patches_swap_in_dummy_credentials_and_the_mock_sdk_inside_the_context(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_FUBON_PERSONAL_ID", _SENTINEL)
        monkeypatch.setenv("HFT_FUBON_API_KEY", _SENTINEL)
        monkeypatch.setenv("HFT_ORDER_MODE", "live")

        with probe._dry_run_patches("TXFL6") as factory:
            assert probe.os.environ["HFT_FUBON_PERSONAL_ID"] == "DRYRUN"
            assert "HFT_FUBON_API_KEY" not in probe.os.environ
            assert "HFT_ORDER_MODE" not in probe.os.environ
            assert facade_module.FubonSDK is factory
            assert probe.validate_probe_target() == probe._DRY_RUN_URL

    def test_dry_run_sdk_factory_checks_constructor_arguments_against_the_real_signature(self) -> None:
        factory = probe._DryRunSdkFactory(fubon_mock_helper, "TXFL6")

        with pytest.raises(TypeError):
            factory(ws_url="wss://neoapitest.fbs.com.tw/x")

        assert factory.calls == []
