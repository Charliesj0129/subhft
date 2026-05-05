"""Slice-D Tasks 11-13: CLI subcommands for screen/kill/cluster.

The three commands wrap the underlying ``screener.cheap_screen``,
``kill_ledger.append_kill``, and ``cluster.cluster_alphas`` helpers. These
tests cover the CLI dispatch layer:

  * argparse plumbing produces the expected ``argparse.Namespace`` shape.
  * empty / whitespace ``--reason`` rejected by ``cmd_alpha_kill``.
  * ``--write-kill`` on the screener actually writes a
    ``gate='pre_screen'`` row (and only when the flag is set).
  * cluster command emits both table and JSON output and propagates
    ``EmptyCorpusError`` to exit code 2.

Underlying-helper unit tests live in
``tests/unit/alpha/test_screener.py``, ``test_kill_ledger.py``, and
``test_cluster.py``; we do not duplicate that math here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from hft_platform.alpha import audit, kill_ledger
from hft_platform.cli._alpha import (
    cmd_alpha_cluster,
    cmd_alpha_kill,
    cmd_alpha_screen,
)
from hft_platform.cli._parser import build_parser

# ---------------------------------------------------------------------------
# Fixtures — kill ledger + audit isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_jsonl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect kill ledger jsonl writes into ``tmp_path``."""
    jsonl = tmp_path / "_kill_ledger.jsonl"
    monkeypatch.setenv("HFT_ALPHA_KILL_LEDGER_PATH", str(jsonl))
    kill_ledger._reset_cache_for_tests()  # noqa: SLF001
    return jsonl


@pytest.fixture(autouse=True)
def _no_ch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the offline jsonl sink — never touch ClickHouse in tests."""
    monkeypatch.setenv("HFT_ALPHA_AUDIT_ENABLED", "0")
    audit._ENABLED = None  # noqa: SLF001 — re-read env on next call


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _extract_json_payload(stdout: str) -> Any:
    """Extract the JSON object emitted by the CLI command from captured stdout.

    The cheap_screen pipeline emits a ``debug`` structlog event before the
    JSON payload (and structlog's default output target is stdout). We strip
    everything before the first ``{`` and parse from there. ``NaN`` is
    tolerated because Python's ``json`` accepts it under default settings.
    """
    start = stdout.find("{")
    if start == -1:
        # Fall back to list payload (cluster --json)
        start = stdout.find("[")
    assert start >= 0, f"no JSON in stdout: {stdout!r}"
    return json.loads(stdout[start:])


def _make_screener_fixture(
    root: Path,
    alpha_id: str,
    *,
    signal: np.ndarray,
    prices: np.ndarray,
) -> None:
    """Write the manifest + signal that ``cheap_screen`` needs under ``root``."""
    alpha_dir = root / "research" / "alphas" / alpha_id
    alpha_dir.mkdir(parents=True, exist_ok=True)
    (alpha_dir / "manifest.yaml").write_text(f"alpha_id: {alpha_id}\n")

    exp_dir = root / "research" / "experiments" / alpha_id
    exp_dir.mkdir(parents=True, exist_ok=True)
    arr = np.column_stack([signal.astype(np.float64), prices.astype(np.float64)])
    np.save(exp_dir / "signal.npy", arr)


# ---------------------------------------------------------------------------
# T12 — cmd_alpha_kill
# ---------------------------------------------------------------------------


def test_alpha_kill_rejects_empty_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()
    args = parser.parse_args(["alpha", "kill", "alpha_x", "--reason", ""])
    with pytest.raises(SystemExit) as exc:
        cmd_alpha_kill(args)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "cannot be empty" in err


def test_alpha_kill_rejects_whitespace_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()
    args = parser.parse_args(["alpha", "kill", "alpha_x", "--reason", "   "])
    with pytest.raises(SystemExit) as exc:
        cmd_alpha_kill(args)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "cannot be empty" in err


def test_alpha_kill_inserts_record(
    _isolated_jsonl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()
    args = parser.parse_args(["alpha", "kill", "alpha_x", "--reason", "promotion blocked: gate D"])
    cmd_alpha_kill(args)  # exits 0 normally (no SystemExit)

    rows = _read_jsonl(_isolated_jsonl)
    assert len(rows) == 1
    assert rows[0]["alpha_id"] == "alpha_x"
    assert rows[0]["gate"] == "manual"
    assert rows[0]["reason"] == "promotion blocked: gate D"
    assert rows[0]["killed_by"] == "cli:kill:operator"

    payload = json.loads(capsys.readouterr().out)
    assert payload["inserted"] is True
    assert payload["alpha_id"] == "alpha_x"
    assert payload["kill_id"]


def test_alpha_kill_idempotent(
    _isolated_jsonl: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = build_parser()
    cmd = ["alpha", "kill", "alpha_x", "--reason", "duplicate test"]
    cmd_alpha_kill(parser.parse_args(cmd))
    capsys.readouterr()  # discard first stdout
    cmd_alpha_kill(parser.parse_args(cmd))

    second_payload = json.loads(capsys.readouterr().out)
    assert second_payload["inserted"] is False

    rows = _read_jsonl(_isolated_jsonl)
    assert len(rows) == 1


def test_alpha_kill_custom_gate_propagates(
    _isolated_jsonl: Path,
) -> None:
    parser = build_parser()
    args = parser.parse_args(["alpha", "kill", "alpha_x", "--reason", "manifest narrowed", "--gate", "F"])
    cmd_alpha_kill(args)
    rows = _read_jsonl(_isolated_jsonl)
    assert len(rows) == 1
    assert rows[0]["gate"] == "F"


# ---------------------------------------------------------------------------
# T11 — cmd_alpha_screen
# ---------------------------------------------------------------------------


def _alternating_signal(n: int = 200) -> np.ndarray:
    """Sign-flip every step → turnover saturates at 2.0 (kill condition)."""
    base = np.arange(n, dtype=np.float64)
    return np.where(base % 2 == 0, 1.0, -1.0)


def _smooth_signal(n: int = 200, *, seed: int = 42) -> np.ndarray:
    """Low-turnover signal (cumulative sum of mean-zero noise)."""
    rng = np.random.default_rng(seed)
    return np.cumsum(rng.standard_normal(n) * 0.01)


def _drifting_prices(n: int = 200, *, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 100.0 + np.cumsum(rng.standard_normal(n) * 0.1)


def test_alpha_screen_kill_writes_ledger_when_flag_set(
    _isolated_jsonl: Path,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    alpha_id = "alpha_high_turnover"
    _make_screener_fixture(
        tmp_path,
        alpha_id,
        signal=_alternating_signal(200),
        prices=_drifting_prices(200),
    )

    parser = build_parser()
    args = parser.parse_args(
        [
            "alpha",
            "screen",
            alpha_id,
            "--project-root",
            str(tmp_path),
            "--write-kill",
        ]
    )
    with pytest.raises(SystemExit) as exc:
        cmd_alpha_screen(args)
    assert exc.value.code == 2

    payload = _extract_json_payload(capsys.readouterr().out)
    assert payload["verdict"] == "kill"
    assert payload["alpha_id"] == alpha_id

    rows = _read_jsonl(_isolated_jsonl)
    assert len(rows) == 1
    assert rows[0]["alpha_id"] == alpha_id
    assert rows[0]["gate"] == "pre_screen"
    assert rows[0]["killed_by"] == "cli:screen"


def test_alpha_screen_kill_does_not_write_ledger_without_flag(
    _isolated_jsonl: Path,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    alpha_id = "alpha_high_turnover"
    _make_screener_fixture(
        tmp_path,
        alpha_id,
        signal=_alternating_signal(200),
        prices=_drifting_prices(200),
    )

    parser = build_parser()
    args = parser.parse_args(["alpha", "screen", alpha_id, "--project-root", str(tmp_path)])
    with pytest.raises(SystemExit) as exc:
        cmd_alpha_screen(args)
    assert exc.value.code == 2

    capsys.readouterr()  # drain
    assert _read_jsonl(_isolated_jsonl) == []


def test_alpha_screen_emits_json(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    alpha_id = "alpha_smooth"
    _make_screener_fixture(
        tmp_path,
        alpha_id,
        signal=_smooth_signal(300),
        prices=_drifting_prices(300),
    )

    parser = build_parser()
    args = parser.parse_args(["alpha", "screen", alpha_id, "--project-root", str(tmp_path)])
    # smooth signal should not trigger a kill — exit normally.
    cmd_alpha_screen(args)

    payload = _extract_json_payload(capsys.readouterr().out)
    assert payload["alpha_id"] == alpha_id
    assert payload["verdict"] in {"pass", "unknown"}
    for key in (
        "ic_mean",
        "ic_std",
        "turnover",
        "cost_floor_breach",
        "reason",
        "duration_s",
    ):
        assert key in payload


def test_alpha_screen_threshold_overrides_plumb_through(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """``--threshold-turnover 0.0`` should kill any non-zero turnover signal."""
    alpha_id = "alpha_smooth"
    _make_screener_fixture(
        tmp_path,
        alpha_id,
        signal=_smooth_signal(300),
        prices=_drifting_prices(300),
    )

    parser = build_parser()
    args = parser.parse_args(
        [
            "alpha",
            "screen",
            alpha_id,
            "--project-root",
            str(tmp_path),
            "--threshold-turnover",
            "0.0",
        ]
    )
    with pytest.raises(SystemExit) as exc:
        cmd_alpha_screen(args)
    assert exc.value.code == 2
    payload = _extract_json_payload(capsys.readouterr().out)
    assert payload["verdict"] == "kill"


# ---------------------------------------------------------------------------
# T13 — cmd_alpha_cluster
# ---------------------------------------------------------------------------


def test_alpha_cluster_empty_corpus_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hft_platform.alpha import cluster as cluster_mod

    def _raise(**_kwargs: Any) -> Any:
        raise cluster_mod.EmptyCorpusError("no alphas")

    monkeypatch.setattr(cluster_mod, "cluster_alphas", _raise)

    parser = build_parser()
    args = parser.parse_args(["alpha", "cluster"])
    with pytest.raises(SystemExit) as exc:
        cmd_alpha_cluster(args)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "empty corpus" in err.lower()


def test_alpha_cluster_table_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hft_platform.alpha import cluster as cluster_mod

    fake = [
        cluster_mod.ClusterAssignment(
            alpha_id="alpha_a",
            cluster_id="cluster_0",
            cluster_size=2,
            max_intra_cluster_corr=0.92,
        ),
        cluster_mod.ClusterAssignment(
            alpha_id="alpha_b",
            cluster_id="cluster_0",
            cluster_size=2,
            max_intra_cluster_corr=0.92,
        ),
    ]
    monkeypatch.setattr(cluster_mod, "cluster_alphas", lambda **_kwargs: fake)

    parser = build_parser()
    args = parser.parse_args(["alpha", "cluster"])
    cmd_alpha_cluster(args)

    out = capsys.readouterr().out
    assert "alpha_id" in out  # header
    assert "cluster_id" in out
    assert "alpha_a" in out
    assert "alpha_b" in out
    assert "cluster_0" in out


def test_alpha_cluster_json_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hft_platform.alpha import cluster as cluster_mod

    fake = [
        cluster_mod.ClusterAssignment(
            alpha_id="alpha_a",
            cluster_id="singleton_alpha_a",
            cluster_size=1,
            max_intra_cluster_corr=0.0,
        ),
    ]
    monkeypatch.setattr(cluster_mod, "cluster_alphas", lambda **_kwargs: fake)

    parser = build_parser()
    args = parser.parse_args(["alpha", "cluster", "--json"])
    cmd_alpha_cluster(args)

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["alpha_id"] == "alpha_a"
    assert payload[0]["cluster_id"] == "singleton_alpha_a"
    assert payload[0]["cluster_size"] == 1
    assert payload[0]["max_intra_cluster_corr"] == 0.0


def test_alpha_cluster_threshold_and_metric_propagate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI must pass --threshold/--metric through to cluster_alphas."""
    from hft_platform.alpha import cluster as cluster_mod

    captured: dict[str, Any] = {}

    def _spy(**kwargs: Any) -> list[Any]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(cluster_mod, "cluster_alphas", _spy)

    parser = build_parser()
    args = parser.parse_args(
        [
            "alpha",
            "cluster",
            "--threshold",
            "0.85",
            "--metric",
            "spearman",
            "--write-artifact",
            "--json",
        ]
    )
    cmd_alpha_cluster(args)

    assert captured["threshold"] == pytest.approx(0.85)
    assert captured["metric"] == "spearman"
    assert captured["write_artifact"] is True
    assert captured["base_dir"] == "research/experiments"
