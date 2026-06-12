"""CLI surface: generate / run / summarize / promote / replay-fallback."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import research.candidate_loop.__main__ as cli

PROMPT = """---
prompt_id: microprice__v1
schema_ref: research/candidate_loop/prompts/v1/candidate.schema.json
primitive_version: prim_v1
---
body
"""


class _StubCH:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, list]] = []

    def query(self, sql: str, parameters: dict | None = None) -> SimpleNamespace:
        return SimpleNamespace(result_rows=[])

    def insert(self, table: str, rows: list, column_names: list[str]) -> None:
        self.inserted.append((table, rows))


class TestGenerate:
    def test_generate_writes_headered_family_file(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        prompt = tmp_path / "microprice.md"
        prompt.write_text(PROMPT)
        drop = tmp_path / "drop.jsonl"
        drop.write_text('{"name": "x"}\n{"name": "y"}\n')
        rcode = cli.main(
            [
                "generate",
                "--run-id", "g1",
                "--family", "microprice",
                "--count", "2",
                "--prompt", str(prompt),
                "--from-jsonl", str(drop),
                "--generation-model", "template_v1",
                "--candidates-root", str(tmp_path / "candidates"),
            ]
        )
        assert rcode == 0
        out_path = tmp_path / "candidates" / "g1" / "family=microprice.jsonl"
        assert out_path.exists()
        header = json.loads(out_path.read_text().splitlines()[0])
        assert header["_header"] is True
        assert header["generation_model"] == "template_v1"
        assert str(out_path) in capsys.readouterr().out


class TestRun:
    def test_run_no_ch_calls_run_batch_and_prints_totals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        seen: dict = {}

        def fake_run_batch(rc, client):  # noqa: ANN001
            seen["rc"] = rc
            seen["client"] = client
            return {"totals": {"candidates": 12, "invalid": 7}}

        monkeypatch.setattr(cli, "run_batch", fake_run_batch)
        rcode = cli.main(
            [
                "run",
                "--batch", "e2e_001",
                "--no-ch",
                "--candidates-root", str(tmp_path / "candidates"),
                "--runs-root", str(tmp_path / "runs"),
            ]
        )
        assert rcode == 0
        assert seen["client"] is None
        assert seen["rc"].run_id == "e2e_001"
        assert seen["rc"].batch_dir == tmp_path / "candidates" / "e2e_001"
        assert '"candidates": 12' in capsys.readouterr().out


class TestSummarize:
    def test_summarize_rebuilds_summary_from_ch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr(cli, "_ch_client", lambda required: _StubCH())
        rcode = cli.main(["summarize", "--batch", "r1", "--runs-root", str(tmp_path / "runs")])
        assert rcode == 0
        path = tmp_path / "runs" / "r1" / "failure_summary.json"
        summary = json.loads(path.read_text())
        assert summary["splits_included"] == ["train", "validation"]
        assert summary["totals"]["candidates"] == 0
        assert str(path) in capsys.readouterr().out


class TestPromote:
    def test_promote_prints_shortlist(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        run_dir = tmp_path / "runs" / "r1"
        run_dir.mkdir(parents=True)
        (run_dir / "failure_summary.json").write_text(
            json.dumps(
                {
                    "promoted": [{"alpha_id": "abc", "family": "microprice", "final_score": 1.5}],
                    "watchlist": [],
                }
            )
        )
        rcode = cli.main(["promote", "--batch", "r1", "--runs-root", str(tmp_path / "runs")])
        assert rcode == 0
        out = capsys.readouterr().out
        assert "PROMOTED (1):" in out
        assert "abc" in out

    def test_promote_missing_summary_is_an_error(self, tmp_path: Path) -> None:
        rcode = cli.main(["promote", "--batch", "nope", "--runs-root", str(tmp_path / "runs")])
        assert rcode == 2


class TestReplayFallback:
    def test_replay_flushes_jsonl_into_ch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        from research.candidate_loop.ch_writer import ResultWriter, compute_result_id

        run_dir = tmp_path / "runs" / "r1"
        offline = ResultWriter(None, run_dir)
        offline.write_result_row(
            {"result_id": compute_result_id("a", "r1", "train", "d", "e", "s"), "alpha_id": "a", "run_id": "r1"}
        )
        ch = _StubCH()
        monkeypatch.setattr(cli, "_ch_client", lambda required: ch)
        rcode = cli.main(["replay-fallback", "--batch", "r1", "--runs-root", str(tmp_path / "runs")])
        assert rcode == 0
        assert len(ch.inserted) == 1
        assert '"inserted": 1' in capsys.readouterr().out


class TestChClient:
    def test_required_client_failure_exits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import hft_platform.infra.ch_client as chmod

        def boom() -> None:
            raise ConnectionError("down")

        monkeypatch.setattr(chmod, "get_ch_client", boom)
        with pytest.raises(SystemExit):
            cli._ch_client(required=True)

    def test_optional_client_failure_degrades_to_none(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import hft_platform.infra.ch_client as chmod

        def boom() -> None:
            raise ConnectionError("down")

        monkeypatch.setattr(chmod, "get_ch_client", boom)
        assert cli._ch_client(required=False) is None
        assert "WARNING" in capsys.readouterr().err
