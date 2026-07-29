import json
import shutil
import uuid
from argparse import Namespace
from pathlib import Path

import numpy as np
import pytest

import hft_platform.cli as cli


def _mine_init_args(*, data_path, out_dir, session_field="session_id", **overrides) -> Namespace:
    base = dict(
        data=str(data_path),
        session_field=session_field,
        embargo_rows=2,
        discovery_ratio=0.50,
        selection_ratio=0.25,
        locked_ratio=0.15,
        holdout_ratio=0.10,
        symbols="TXFD6",
        out_dir=str(out_dir),
        seed=42,
        out=None,
    )
    base.update(overrides)
    return Namespace(**base)


def _structured_data(n_sessions: int = 20, rows_per_session: int = 10) -> np.ndarray:
    n = n_sessions * rows_per_session
    arr = np.zeros(n, dtype=[("session_id", "i8"), ("ofi", "f8")])
    arr["session_id"] = np.repeat(np.arange(n_sessions), rows_per_session)
    arr["ofi"] = np.linspace(-1.0, 1.0, n)
    return arr


def test_cmd_alpha_mine_init_writes_manifest_and_prints_payload(capsys, tmp_path):
    data_path = tmp_path / "sessions.npy"
    np.save(data_path, _structured_data())
    out_dir = tmp_path / "manifest"

    args = _mine_init_args(data_path=data_path, out_dir=out_dir)
    cli.cmd_alpha_mine_init(args)

    payload = json.loads(capsys.readouterr().out)
    assert payload["partition_manifest_path"]
    assert payload["manifest_hash"]
    assert payload["dataset_fingerprint"]
    assert set(payload["partitions"]) == {"discovery", "selection", "locked_validation", "final_holdout"}
    assert payload["partitions"]["discovery"]["post_embargo_row_count"] == 98

    manifest_path = payload["partition_manifest_path"]
    written = json.loads(open(manifest_path).read())
    assert written["manifest_hash"] == payload["manifest_hash"]
    assert written["symbols"] == ["TXFD6"]


def test_cmd_alpha_mine_init_rejects_non_structured_array(capsys, tmp_path):
    data_path = tmp_path / "flat.npy"
    np.save(data_path, np.zeros(10, dtype=np.float64))
    out_dir = tmp_path / "manifest"

    args = _mine_init_args(data_path=data_path, out_dir=out_dir)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_alpha_mine_init(args)
    assert exc.value.code == 2


def test_cmd_alpha_mine_init_rejects_missing_session_field(capsys, tmp_path):
    data_path = tmp_path / "sessions.npy"
    np.save(data_path, _structured_data())
    out_dir = tmp_path / "manifest"

    args = _mine_init_args(data_path=data_path, out_dir=out_dir, session_field="not_a_field")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_alpha_mine_init(args)
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# `hft alpha mine promote` (Phase 4: pipeline integration)
# ---------------------------------------------------------------------------

_ALPHAS_ROOT = Path("research/alphas")


@pytest.fixture()
def scratch_alpha_id():
    alpha_id = f"zz_test_cli_mine_promote_{uuid.uuid4().hex[:8]}"
    yield alpha_id
    alpha_dir = _ALPHAS_ROOT / alpha_id
    if alpha_dir.exists():
        shutil.rmtree(alpha_dir)


def _mine_promote_args(*, alpha_id, expression=None, from_results=None, rank=None, **overrides) -> Namespace:
    base = dict(
        alpha_id=alpha_id,
        owner="charlie",
        strategy_type="taker",
        instrument="TMFD6",
        force=False,
        expression=expression,
        from_results=from_results,
        rank=rank,
    )
    base.update(overrides)
    return Namespace(**base)


def test_parser_mine_promote_requires_expression_or_from_results() -> None:
    from hft_platform.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["alpha", "mine", "promote", "--alpha-id", "x", "--owner", "c", "--instrument", "TMFD6"])


def test_parser_mine_promote_rejects_both_expression_and_from_results() -> None:
    from hft_platform.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "alpha",
                "mine",
                "promote",
                "--alpha-id",
                "x",
                "--owner",
                "c",
                "--instrument",
                "TMFD6",
                "--expression",
                "add(x, y)",
                "--from-results",
                "results.json",
            ]
        )


def test_parser_mine_promote_parses_expression_flags() -> None:
    from hft_platform.cli import build_parser, cmd_alpha_mine_promote

    parser = build_parser()
    args = parser.parse_args(
        [
            "alpha",
            "mine",
            "promote",
            "--alpha-id",
            "zz_test_x",
            "--owner",
            "charlie",
            "--instrument",
            "TMFD6",
            "--expression",
            "add(x, y)",
        ]
    )
    assert args.expression == "add(x, y)"
    assert args.from_results is None
    assert args.func is cmd_alpha_mine_promote


def test_cmd_alpha_mine_promote_writes_alpha_package(capsys, scratch_alpha_id) -> None:
    args = _mine_promote_args(alpha_id=scratch_alpha_id, expression="add(x, y)")
    cli.cmd_alpha_mine_promote(args)
    out = capsys.readouterr().out
    assert "Promoted GP candidate to" in out
    assert (Path("research/alphas") / scratch_alpha_id / "manifest.yaml").exists()


@pytest.mark.skip(
    reason=(
        "promote_from_results() reads SearchResult.selection_sharpe, which only exists in the "
        "GP-search-provenance revision of research/combinatorial/search_engine.py. That revision "
        "also changes what the field computes (sharpe -> ratio), so it was deliberately excluded "
        "from this mining-only branch rather than imported unverified. Phase 2.3 rewrites this "
        "scaffold path for non-SMMA families and must re-enable this test."
    )
)
def test_cmd_alpha_mine_promote_from_results(capsys, scratch_alpha_id, tmp_path) -> None:
    results_path = tmp_path / "results.json"
    results_path.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "expression": "mul(x, y)",
                        "score": 1.0,
                        "selection_sharpe": 0.5,
                        "correlation_pool_max": 0.1,
                        "passed": True,
                        "metadata": {},
                    }
                ]
            }
        )
    )
    args = _mine_promote_args(alpha_id=scratch_alpha_id, from_results=str(results_path), rank=0)
    cli.cmd_alpha_mine_promote(args)
    out = capsys.readouterr().out
    assert "Promoted GP candidate to" in out


def test_cmd_alpha_mine_promote_requires_rank_with_from_results(scratch_alpha_id, tmp_path) -> None:
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps({"results": []}))
    args = _mine_promote_args(alpha_id=scratch_alpha_id, from_results=str(results_path), rank=None)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_alpha_mine_promote(args)
    assert exc.value.code == 2


def test_cmd_alpha_mine_promote_neither_source_exits_2(scratch_alpha_id) -> None:
    args = _mine_promote_args(alpha_id=scratch_alpha_id)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_alpha_mine_promote(args)
    assert exc.value.code == 2


def test_cmd_alpha_mine_promote_refuses_overwrite_without_force(scratch_alpha_id) -> None:
    args = _mine_promote_args(alpha_id=scratch_alpha_id, expression="add(x, y)")
    cli.cmd_alpha_mine_promote(args)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_alpha_mine_promote(args)
    assert exc.value.code == 2


def test_cmd_alpha_mine_promote_rejects_noncausal_expression(scratch_alpha_id) -> None:
    args = _mine_promote_args(alpha_id=scratch_alpha_id, expression="rank(x)")
    with pytest.raises(SystemExit) as exc:
        cli.cmd_alpha_mine_promote(args)
    assert exc.value.code == 2


def test_cmd_alpha_mine_promote_missing_results_file_exits_cleanly(scratch_alpha_id, tmp_path, capsys) -> None:
    missing_path = tmp_path / "does_not_exist.json"
    args = _mine_promote_args(alpha_id=scratch_alpha_id, from_results=str(missing_path), rank=0)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_alpha_mine_promote(args)
    assert exc.value.code == 2
    assert "[hft alpha mine promote]" in capsys.readouterr().out


def test_cmd_alpha_mine_promote_malformed_results_row_exits_cleanly(scratch_alpha_id, tmp_path, capsys) -> None:
    results_path = tmp_path / "malformed_results.json"
    results_path.write_text(json.dumps({"results": [{"expression": "add(x, y)"}]}))  # missing score/etc.
    args = _mine_promote_args(alpha_id=scratch_alpha_id, from_results=str(results_path), rank=0)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_alpha_mine_promote(args)
    assert exc.value.code == 2
    assert "[hft alpha mine promote]" in capsys.readouterr().out
