"""Unit tests for src/hft_platform/cli/_ops.py — coverage-focused."""

from __future__ import annotations

import argparse
import json
import os
from unittest.mock import MagicMock, patch

import pytest


def _ns(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


from hft_platform.cli._ops import cmd_feed_status


class TestCmdFeedStatus:
    def test_metrics_reachable_with_feed_metric(self, capsys):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"feed_events_total 42\n"
        with patch("urllib.request.urlopen", return_value=mock_resp):
            cmd_feed_status(_ns(port=9090))
        out = capsys.readouterr().out
        assert "feed metric present=True" in out

    def test_metrics_reachable_without_feed_metric(self, capsys):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"some_other_metric 1\n"
        with patch("urllib.request.urlopen", return_value=mock_resp):
            cmd_feed_status(_ns(port=9090))
        out = capsys.readouterr().out
        assert "feed metric present=False" in out

    def test_metrics_unreachable_prints_error(self, capsys):
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            cmd_feed_status(_ns(port=9090))
        out = capsys.readouterr().out
        assert "Unable to reach metrics" in out


from hft_platform.cli._ops import cmd_diag


class TestCmdDiag:
    def test_no_trace_file_prints_stub(self, capsys):
        cmd_diag(_ns(trace_file=None))
        out = capsys.readouterr().out
        assert "Diag:" in out
        assert "metrics" in out

    def test_trace_file_json_timeline(self, capsys, tmp_path):
        """With trace file and --timeline (json format), prints JSON timeline."""
        out_file = tmp_path / "timeline.json"
        args = _ns(
            trace_file="some.jsonl",
            trace_id=None,
            stage=None,
            timeline=True,
            timeline_format="json",
            out=str(out_file),
            limit=0,
        )
        fake_replay_mod = MagicMock()
        fake_replay_mod.load_traces.return_value = [{"ts_ns": "1000", "event": "tick"}]
        fake_replay_mod.filter_traces.return_value = [{"ts_ns": "1000", "event": "tick"}]
        fake_replay_mod.build_timeline.return_value = [{"ts": 1000, "event": "tick"}]
        fake_replay_mod.render_timeline_markdown.return_value = "# timeline"
        fake_replay_mod.summarize_trace.return_value = {"count": 1}
        with patch.dict("sys.modules", {"hft_platform.diagnostics.replay": fake_replay_mod}):
            cmd_diag(args)
        out = capsys.readouterr().out
        assert "tick" in out
        assert out_file.exists()

    def test_trace_file_markdown_timeline(self, capsys, tmp_path):
        out_file = tmp_path / "timeline.md"
        args = _ns(
            trace_file="some.jsonl",
            trace_id=None,
            stage=None,
            timeline=True,
            timeline_format="md",
            out=str(out_file),
            limit=0,
        )
        fake_replay_mod = MagicMock()
        fake_replay_mod.load_traces.return_value = []
        fake_replay_mod.filter_traces.return_value = []
        fake_replay_mod.build_timeline.return_value = []
        fake_replay_mod.render_timeline_markdown.return_value = "# md timeline"
        fake_replay_mod.summarize_trace.return_value = {}
        with patch.dict("sys.modules", {"hft_platform.diagnostics.replay": fake_replay_mod}):
            cmd_diag(args)
        out = capsys.readouterr().out
        assert "# md timeline" in out
        assert out_file.exists()

    def test_trace_file_summary_mode(self, capsys):
        """No timeline flag — prints summary + recent records."""
        args = _ns(
            trace_file="some.jsonl",
            trace_id=None,
            stage=None,
            timeline=False,
            limit=2,
        )
        records = [{"ts_ns": "1000", "event": "a"}, {"ts_ns": "2000", "event": "b"}]
        fake_replay_mod = MagicMock()
        fake_replay_mod.load_traces.return_value = records
        fake_replay_mod.filter_traces.return_value = records
        fake_replay_mod.summarize_trace.return_value = {"total": 2}
        with patch.dict("sys.modules", {"hft_platform.diagnostics.replay": fake_replay_mod}):
            cmd_diag(args)
        out = capsys.readouterr().out
        assert "total" in out
        assert "Last records" in out


from hft_platform.cli._ops import cmd_contracts_status


class TestCmdContractsStatus:
    def test_file_not_found_exits_1(self, tmp_path, capsys):
        missing = tmp_path / "nope.json"
        with pytest.raises(SystemExit) as exc_info:
            cmd_contracts_status(_ns(contracts=str(missing), stale_after_s=3600, status_file=None))
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["exists"] is False

    def test_invalid_json_exits_1(self, tmp_path, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            cmd_contracts_status(_ns(contracts=str(bad), stale_after_s=3600, status_file=None))
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "error" in data

    def test_fresh_contract_file(self, tmp_path, capsys):
        import datetime as dt

        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        payload = {
            "updated_at": now_iso,
            "cache_version": 3,
            "contracts": [{"symbol": "2330"}, {"symbol": "TXFD6"}],
        }
        f = tmp_path / "contracts.json"
        f.write_text(json.dumps(payload), encoding="utf-8")
        cmd_contracts_status(_ns(contracts=str(f), stale_after_s=3600, status_file=None))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["exists"] is True
        assert data["contract_count"] == 2
        assert data["cache_version"] == 3
        assert data["stale"] is False

    def test_stale_contract_file(self, tmp_path, capsys):
        import datetime as dt

        old_iso = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
        payload = {"updated_at": old_iso, "cache_version": 1, "contracts": []}
        f = tmp_path / "contracts.json"
        f.write_text(json.dumps(payload), encoding="utf-8")
        cmd_contracts_status(_ns(contracts=str(f), stale_after_s=3600, status_file=None))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["stale"] is True

    def test_env_override_stale_after(self, tmp_path, capsys):
        import datetime as dt

        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        payload = {"updated_at": now_iso, "contracts": []}
        f = tmp_path / "contracts.json"
        f.write_text(json.dumps(payload), encoding="utf-8")
        with patch.dict(os.environ, {"HFT_CONTRACT_REFRESH_S": "10"}):
            cmd_contracts_status(_ns(contracts=str(f), stale_after_s=3600, status_file=None))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["stale_after_s"] == 10.0

    def test_status_file_included_when_present(self, tmp_path, capsys):
        import datetime as dt

        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        payload = {"updated_at": now_iso, "contracts": []}
        f = tmp_path / "contracts.json"
        f.write_text(json.dumps(payload), encoding="utf-8")

        status_file = tmp_path / "status.json"
        status_file.write_text(json.dumps({"running": True}), encoding="utf-8")

        cmd_contracts_status(_ns(contracts=str(f), stale_after_s=3600, status_file=str(status_file)))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["runtime_status"]["running"] is True


from hft_platform.cli._ops import cmd_recorder_status


class TestCmdRecorderStatus:
    def _make_args(self, wal_dir="data/wal", ck_host="localhost"):
        return _ns(wal_dir=wal_dir, ck_host=ck_host)

    def test_wal_dir_missing_and_ck_unreachable(self, capsys, tmp_path):
        missing_dir = str(tmp_path / "missing_wal")
        args = self._make_args(wal_dir=missing_dir)
        with patch("urllib.request.urlopen", side_effect=Exception("unreachable")):
            with patch("os.statvfs") as mock_statvfs:
                mock_vfs = MagicMock()
                mock_vfs.f_frsize = 4096
                mock_vfs.f_bavail = 1024 * 1024 * 1024
                mock_statvfs.return_value = mock_vfs
                cmd_recorder_status(args)
        out = capsys.readouterr().out
        assert "WAL Status:" in out
        assert "0 files" in out
        assert "unreachable" in out

    def test_wal_files_present(self, capsys, tmp_path):
        wal_dir = tmp_path / "wal"
        wal_dir.mkdir()
        wal1 = wal_dir / "2026-01-01.wal"
        wal1.write_bytes(b"x" * 1024)
        wal2 = wal_dir / "2026-01-02.wal"
        wal2.write_bytes(b"x" * 2048)

        args = self._make_args(wal_dir=str(wal_dir))
        with patch("urllib.request.urlopen", side_effect=Exception("no ck")):
            with patch("os.statvfs") as mock_statvfs:
                mock_vfs = MagicMock()
                mock_vfs.f_frsize = 4096
                mock_vfs.f_bavail = 1024 * 1024 * 1024
                mock_statvfs.return_value = mock_vfs
                cmd_recorder_status(args)
        out = capsys.readouterr().out
        assert "2 files" in out
        assert "KB" in out or "MB" in out or "B" in out

    def test_ck_reachable(self, capsys, tmp_path):
        wal_dir = tmp_path / "wal"
        wal_dir.mkdir()
        args = self._make_args(wal_dir=str(wal_dir))

        mock_resp = MagicMock()
        mock_resp.status = 200
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch("os.statvfs") as mock_statvfs:
                mock_vfs = MagicMock()
                mock_vfs.f_frsize = 4096
                mock_vfs.f_bavail = 256 * 1024
                mock_statvfs.return_value = mock_vfs
                cmd_recorder_status(args)
        out = capsys.readouterr().out
        assert "ok" in out

    def test_disk_guard_active_when_low_space(self, capsys, tmp_path):
        wal_dir = tmp_path / "wal"
        wal_dir.mkdir()
        args = self._make_args(wal_dir=str(wal_dir))

        with patch("urllib.request.urlopen", side_effect=Exception("no ck")):
            with patch("os.statvfs") as mock_statvfs:
                with patch.dict(os.environ, {"HFT_WAL_DISK_MIN_MB": "1000"}):
                    mock_vfs = MagicMock()
                    mock_vfs.f_frsize = 4096
                    mock_vfs.f_bavail = 100 * 1024 * 1024 // 4096
                    mock_statvfs.return_value = mock_vfs
                    cmd_recorder_status(args)
        out = capsys.readouterr().out
        assert "ACTIVE" in out


from hft_platform.cli._ops import cmd_ops_rearm_strategy


class TestCmdOpsRearmStrategy:
    def test_rearm_strategy_reports_a_request_not_a_completed_rearm(self, capsys):
        """The old message claimed "Strategy re-armed" while nothing consumed it.

        That false success is how a dead strategy looked recovered to the
        operator who ran the command -- R47_MAKER_TMF sat quarantined for 33 h
        behind exactly this line. The output must describe what happened.
        """
        gate = MagicMock()
        gate.rearm_strategy.return_value = "abc123"
        with patch("hft_platform.cli._ops.ManualRearmGate", return_value=gate):
            cmd_ops_rearm_strategy(_ns(strategy_id="strat1", state_path=None))
        gate.rearm_strategy.assert_called_once_with("strat1")
        out = capsys.readouterr().out
        assert "requested" in out.lower()
        assert "abc123" in out
        assert "strat1" in out

    def test_rearm_strategy_value_error_exits_1(self, capsys):
        gate = MagicMock()
        gate.rearm_strategy.side_effect = ValueError("not required")
        with patch("hft_platform.cli._ops.ManualRearmGate", return_value=gate):
            with pytest.raises(SystemExit) as exc_info:
                cmd_ops_rearm_strategy(_ns(strategy_id="strat1", state_path=None))
        assert exc_info.value.code == 1


from hft_platform.cli._ops import cmd_ops_rearm_platform


class TestCmdOpsRearmPlatform:
    def test_rearm_platform_passes_token_reason_and_actor(self, capsys):
        gate = MagicMock()
        with patch("hft_platform.cli._ops.ManualRearmGate", return_value=gate):
            cmd_ops_rearm_platform(_ns(state_path=None, token="abc", reason="disk freed"))
        kwargs = gate.rearm_platform.call_args.kwargs
        assert (kwargs["token"], kwargs["reason"]) == ("abc", "disk freed")
        assert kwargs["actor"].startswith("cli:")
        out = capsys.readouterr().out
        assert "re-arm requested" in out.lower()

    def test_rearm_platform_refused_exits_1(self, capsys):
        gate = MagicMock()
        gate.rearm_platform.side_effect = ValueError("token 'x' does not match")
        with patch("hft_platform.cli._ops.ManualRearmGate", return_value=gate):
            with pytest.raises(SystemExit) as exc_info:
                cmd_ops_rearm_platform(_ns(state_path=None, token="x", reason="r"))
        assert exc_info.value.code == 1
        assert "does not match" in capsys.readouterr().err


from hft_platform.cli._ops import cmd_ops_autonomy_status


class TestCmdOpsAutonomyStatus:
    def test_prints_snapshot_json(self, capsys):
        gate = MagicMock()
        gate.snapshot.return_value = {"platform": {"manual_rearm_required": False}, "strategies": {}}
        gate.platform_token.return_value = "tok123"
        with patch("hft_platform.cli._ops.ManualRearmGate", return_value=gate):
            cmd_ops_autonomy_status(_ns(state_path=None))
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "platform" in data
        assert data["platform_token"] == "tok123"


from hft_platform.cli._ops import FlattenStatus, _flatten_via_gate


class TestFlattenViaGate:
    def test_immediate_completion(self):
        gate = MagicMock()
        req = MagicMock()
        req.status = FlattenStatus.COMPLETED
        gate.read_request.return_value = req
        result = _flatten_via_gate("all", None, 120, gate=gate, poll_timeout_s=1.0)
        assert result is req

    def test_immediate_failure(self):
        gate = MagicMock()
        req = MagicMock()
        req.status = FlattenStatus.FAILED
        gate.read_request.return_value = req
        result = _flatten_via_gate("all", None, 120, gate=gate, poll_timeout_s=1.0)
        assert result is req

    def test_timeout_returns_none(self, capsys):
        gate = MagicMock()
        pending_req = MagicMock()
        pending_req.status = FlattenStatus.PENDING
        gate.read_request.return_value = pending_req
        result = _flatten_via_gate("all", None, 120, gate=gate, poll_timeout_s=0.01)
        assert result is None
        out = capsys.readouterr().out
        assert "Timeout" in out

    def test_none_request_keeps_polling(self):
        gate = MagicMock()
        completed_req = MagicMock()
        completed_req.status = FlattenStatus.COMPLETED
        gate.read_request.side_effect = [None, completed_req]
        with patch("time.sleep"):
            result = _flatten_via_gate("all", None, 120, gate=gate, poll_timeout_s=5.0)
        assert result is completed_req


from hft_platform.cli._ops import cmd_ops_flatten


class TestCmdOpsFlatten:
    def test_flatten_all_closed_prints_result_and_exits_0(self, capsys):
        req = MagicMock()
        req.status = FlattenStatus.COMPLETED
        req.fully_closed = 2
        req.partially_closed = 0
        req.failed = 0
        req.failed_symbols = []
        with patch("hft_platform.cli._ops._flatten_via_gate", return_value=req):
            cmd_ops_flatten(_ns(scope="all", scope_id=None, deadline=120))
        out = capsys.readouterr().out
        assert "finished" in out.lower()
        assert "fully_closed=2" in out

    def test_flatten_partially_closed_exits_1(self, capsys):
        req = MagicMock()
        req.status = FlattenStatus.COMPLETED
        req.fully_closed = 1
        req.partially_closed = 1
        req.failed = 0
        req.failed_symbols = ["TMFK6"]
        with patch("hft_platform.cli._ops._flatten_via_gate", return_value=req):
            with pytest.raises(SystemExit) as exc_info:
                cmd_ops_flatten(_ns(scope="all", scope_id=None, deadline=120))
        assert exc_info.value.code == 1
        assert "Still open: TMFK6" in capsys.readouterr().out

    def test_flatten_completed_with_failed_symbols(self, capsys):
        req = MagicMock()
        req.status = FlattenStatus.COMPLETED
        req.fully_closed = 0
        req.partially_closed = 0
        req.failed = 1
        req.failed_symbols = ["TXFD6"]
        with patch("hft_platform.cli._ops._flatten_via_gate", return_value=req):
            with pytest.raises(SystemExit) as exc_info:
                cmd_ops_flatten(_ns(scope="all", scope_id=None, deadline=120))
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "TXFD6" in out

    def test_flatten_timeout_exits_1(self, capsys):
        with patch("hft_platform.cli._ops._flatten_via_gate", return_value=None):
            with pytest.raises(SystemExit) as exc_info:
                cmd_ops_flatten(_ns(scope="all", scope_id=None, deadline=120))
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "no result from the engine" in out.lower()

    def test_flatten_failed_exits_1(self, capsys):
        req = MagicMock()
        req.status = FlattenStatus.FAILED
        req.error = "Engine halted"
        with patch("hft_platform.cli._ops._flatten_via_gate", return_value=req):
            with pytest.raises(SystemExit) as exc_info:
                cmd_ops_flatten(_ns(scope="all", scope_id=None, deadline=120))
        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "Engine halted" in out


from hft_platform.cli._ops import cmd_strat_test


class TestCmdStratTest:
    def test_import_failure_exits_1(self, capsys):
        """If strategy module can't be imported, exit code 1."""
        with (
            patch("hft_platform.cli._ops.load_settings") as mock_settings,
            patch("hft_platform.cli._ops.import_module", side_effect=ImportError("no such module")),
        ):
            mock_settings.return_value = ({}, None)
            with pytest.raises(SystemExit) as exc_info:
                cmd_strat_test(
                    _ns(
                        module="hft_platform.strategies.nonexistent",
                        cls="FakeClass",
                        strategy_id="demo",
                        symbol="2330",
                    )
                )
        assert exc_info.value.code == 1


from hft_platform.cli._ops import cmd_backtest


class TestCmdBacktest:
    def test_no_backtest_cmd_exits_1(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            cmd_backtest(_ns(backtest_cmd=None))
        assert exc_info.value.code == 1

    def test_convert_success(self, capsys):
        fake_mod = MagicMock()
        with patch.dict("sys.modules", {"hft_platform.backtest.convert": fake_mod}):
            cmd_backtest(_ns(backtest_cmd="convert", input="in.jsonl", output="out.npz", scale=10000))
        out = capsys.readouterr().out
        assert "Converted" in out

    def test_convert_failure_exits_1(self, capsys):
        fake_mod = MagicMock()
        fake_mod.convert_jsonl_to_npz.side_effect = Exception("convert error")
        with patch.dict("sys.modules", {"hft_platform.backtest.convert": fake_mod}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_backtest(_ns(backtest_cmd="convert", input="in.jsonl", output="out.npz", scale=10000))
        assert exc_info.value.code == 1

    def test_run_import_failure_exits_1(self, capsys):
        with patch.dict("sys.modules", {"hft_platform.backtest.adapter": None}):
            with pytest.raises(SystemExit) as exc_info:
                cmd_backtest(
                    _ns(
                        backtest_cmd="run",
                        strategy_module=None,
                        data=["data.npz"],
                        symbols=["2330"],
                        tick_sizes=None,
                        tick_size=1,
                        lot_sizes=None,
                        lot_size=1000,
                        latency_entry=36,
                        latency_resp=36,
                        fee_maker=0.0,
                        fee_taker=0.0,
                        no_partial_fill=False,
                        strict_equity=False,
                        record_out=None,
                        report=False,
                        seed=42,
                    )
                )
        assert exc_info.value.code == 1

    def test_run_multiple_data_files_with_strategy_exits_1(self, capsys):
        fake_adapter_mod = MagicMock()
        fake_runner_mod = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "hft_platform.backtest.adapter": fake_adapter_mod,
                "hft_platform.backtest.runner": fake_runner_mod,
            },
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_backtest(
                    _ns(
                        backtest_cmd="run",
                        strategy_module="some.module",
                        strategy_class="Strat",
                        strategy_id="demo",
                        symbol="2330",
                        data=["a.npz", "b.npz"],
                        tick_size=1,
                        lot_size=1000,
                        fee_maker=0.0,
                        fee_taker=0.0,
                        no_partial_fill=False,
                        price_scale=10000,
                        timeout=60,
                        seed=42,
                    )
                )
        assert exc_info.value.code == 1

    def test_run_no_strategy_multiple_data_exits_1(self, capsys):
        fake_adapter_mod = MagicMock()
        fake_runner_mod = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "hft_platform.backtest.adapter": fake_adapter_mod,
                "hft_platform.backtest.runner": fake_runner_mod,
            },
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_backtest(
                    _ns(
                        backtest_cmd="run",
                        strategy_module=None,
                        data=["a.npz", "b.npz"],
                        symbols=["2330"],
                        tick_sizes=None,
                        tick_size=1,
                        lot_sizes=None,
                        lot_size=1000,
                        latency_entry=36,
                        latency_resp=36,
                        fee_maker=0.0,
                        fee_taker=0.0,
                        no_partial_fill=False,
                        strict_equity=False,
                        record_out=None,
                        report=False,
                        seed=42,
                    )
                )
        assert exc_info.value.code == 1

    def test_run_no_strategy_runner_returns_none_exits_1(self, capsys):
        fake_adapter_mod = MagicMock()
        fake_runner_cls = MagicMock()
        runner_instance = MagicMock()
        runner_instance.run.return_value = None
        fake_runner_cls.HftBacktestRunner.return_value = runner_instance
        fake_runner_cls.HftBacktestConfig = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "hft_platform.backtest.adapter": fake_adapter_mod,
                "hft_platform.backtest.runner": fake_runner_cls,
            },
        ):
            with pytest.raises(SystemExit) as exc_info:
                cmd_backtest(
                    _ns(
                        backtest_cmd="run",
                        strategy_module=None,
                        data=["a.npz"],
                        symbols=["2330"],
                        tick_sizes=None,
                        tick_size=1,
                        lot_sizes=None,
                        lot_size=1000,
                        latency_entry=36,
                        latency_resp=36,
                        fee_maker=0.0,
                        fee_taker=0.0,
                        no_partial_fill=False,
                        strict_equity=False,
                        record_out=None,
                        report=False,
                        seed=42,
                    )
                )
        assert exc_info.value.code == 1

    def test_run_no_strategy_success(self, capsys):
        fake_adapter_mod = MagicMock()
        fake_runner_mod = MagicMock()
        result = MagicMock()
        result.run_id = "r001"
        result.config_hash = "abc123"
        result.pnl = 1500.0
        result.used_synthetic_equity = False
        result.equity_points = [0.0, 1.0, 2.0]
        fake_runner_mod.HftBacktestRunner.return_value.run.return_value = result
        fake_runner_mod.HftBacktestConfig = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "hft_platform.backtest.adapter": fake_adapter_mod,
                "hft_platform.backtest.runner": fake_runner_mod,
            },
        ):
            cmd_backtest(
                _ns(
                    backtest_cmd="run",
                    strategy_module=None,
                    data=["a.npz"],
                    symbols=["2330"],
                    tick_sizes=None,
                    tick_size=1,
                    lot_sizes=None,
                    lot_size=1000,
                    latency_entry=36,
                    latency_resp=36,
                    fee_maker=0.0,
                    fee_taker=0.0,
                    no_partial_fill=False,
                    strict_equity=False,
                    record_out=None,
                    report=False,
                    seed=42,
                )
            )
        out = capsys.readouterr().out
        assert "Backtest completed" in out
        assert "r001" in out
