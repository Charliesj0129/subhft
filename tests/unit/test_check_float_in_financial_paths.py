"""The float gate for money paths: explicit exemptions only, no substring escapes.

The CI step it replaces grepped ``file:line:text`` through ~49 ``grep -v`` substrings, so a word in a path
(``normalizer``) or in a comment (``precision-``) exempted a whole line or a whole file. These tests pin the
properties the pipeline lacked.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_float_in_financial_paths.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("check_float_in_financial_paths", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_module()

PKG = "src/hft_platform"


def _tree(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    """A repo with every scan target present and empty, plus *files* (paths relative to ``src/hft_platform``)."""
    root = tmp_path / "repo"
    for target in ("order", "risk", "execution"):
        (root / PKG / target).mkdir(parents=True)
    (root / PKG / "feed_adapter").mkdir(parents=True)
    (root / PKG / "feed_adapter" / "normalizer.py").write_text("", encoding="utf-8")
    for rel, body in (files or {}).items():
        path = root / PKG / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def _exemptions(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "exemptions.yaml"
    path.write_text(yaml.safe_dump({"exemptions": entries}), encoding="utf-8")
    return path


def _entry(file: str, code: str, **extra) -> dict:
    return {"file": f"{PKG}/{file}", "code": code, "kind": "not-money", "reason": "a duration in seconds", **extra}


def _run(root: Path, exemptions: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = gate.main(["--root", str(root), "--exemptions", str(exemptions)])
    out = capsys.readouterr()
    return code, out.out, out.err


# --- what is flagged -----------------------------------------------------------------------------------------------


def test_an_unexempted_float_annotation_and_call_are_flagged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, {"order/a.py": "price: float = 0.0\nfee = float(raw)\nok = 1\n"})

    code, _, err = _run(root, _exemptions(tmp_path, []), capsys)

    assert code == 1
    assert f"{PKG}/order/a.py:1: price: float = 0.0" in err
    assert f"{PKG}/order/a.py:2: fee = float(raw)" in err
    assert "a.py:3" not in err


def test_a_word_like_latency_in_the_identifier_no_longer_exempts_a_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The old pipeline dropped any line containing 'latency', 'timestamp', 'threshold', ... anywhere."""
    root = _tree(tmp_path, {"risk/a.py": "latency_price: float = 0.0\nthreshold_fee = float(x)\n"})

    code, _, err = _run(root, _exemptions(tmp_path, []), capsys)

    assert code == 1
    assert "latency_price" in err and "threshold_fee" in err


def test_a_file_named_normalizer_under_execution_is_not_exempt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``grep -v normalizer`` matched the path, so every float in ``execution/normalizer.py`` vanished."""
    root = _tree(tmp_path, {"execution/normalizer.py": "price: float = 0.0\n"})

    code, _, err = _run(root, _exemptions(tmp_path, []), capsys)

    assert code == 1
    assert "execution/normalizer.py:1" in err


def test_the_feed_adapter_normalizer_is_scanned(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, {"feed_adapter/normalizer.py": "def f(x):\n    return float(x)\n"})

    code, _, err = _run(root, _exemptions(tmp_path, []), capsys)

    assert code == 1
    assert "feed_adapter/normalizer.py:2" in err


def test_nested_packages_under_a_scan_target_are_scanned(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, {"order/sub/deeper/a.py": "price: float = 0.0\n"})

    code, _, err = _run(root, _exemptions(tmp_path, []), capsys)

    assert code == 1
    assert "order/sub/deeper/a.py:1" in err


def test_files_outside_the_scan_targets_are_not_scanned(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, {"feed_adapter/other.py": "x = float(1)\n", "alpha/a.py": "x: float = 1.0\n"})

    code, out, _ = _run(root, _exemptions(tmp_path, []), capsys)

    assert code == 0
    assert "float-gate: OK" in out


def test_a_float_mentioned_only_in_a_comment_is_ignored(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, {"order/a.py": "x = 1  # was: price: float, then float(raw)\n"})

    code, out, _ = _run(root, _exemptions(tmp_path, []), capsys)

    assert code == 0
    assert "float-gate: OK" in out


# --- inline markers --------------------------------------------------------------------------------------------------


def test_an_inline_marker_with_a_reason_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, {"order/a.py": "ttl: float = 5.0  # precision-ok: seconds, not an amount\n"})

    code, out, _ = _run(root, _exemptions(tmp_path, []), capsys)

    assert code == 0
    assert "inline-marker=1" in out


@pytest.mark.parametrize(
    "comment", ["# precision-ok", "# precision-ok:", "# precision-ok:   ", "# precision-ok seconds"]
)
def test_a_marker_without_a_reason_is_reported_as_needing_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], comment: str
) -> None:
    root = _tree(tmp_path, {"order/a.py": f"ttl: float = 5.0  {comment}\n"})

    code, _, err = _run(root, _exemptions(tmp_path, []), capsys)

    assert code == 1
    assert "order/a.py:1" in err
    assert "'# precision-ok' needs ': <reason>'" in err


@pytest.mark.parametrize("comment", ["# precision- seconds", "# precision-config", "# noqa", "# precision-okay: x"])
def test_a_look_alike_comment_does_not_exempt(tmp_path: Path, capsys: pytest.CaptureFixture[str], comment: str) -> None:
    root = _tree(tmp_path, {"order/a.py": f"ttl: float = 5.0  {comment}\n"})

    code, _, err = _run(root, _exemptions(tmp_path, []), capsys)

    assert code == 1
    assert "order/a.py:1" in err


@pytest.mark.parametrize("separator", [" ", " ", "\x85", "\x0b", "\x0c", "\x1c"])
def test_a_line_separator_inside_a_string_does_not_move_a_marker_to_the_line_above(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], separator: str
) -> None:
    """``str.splitlines()`` breaks on these characters and the tokenizer does not: the lines after one were numbered
    one too high, so the unmarked float on line 3 took the ``# precision-ok`` comment of line 4 and passed."""
    body = f'DOC = "a{separator}b"\nx = 1\nprice: float = 0.0\nttl: float = 1.0  # precision-ok: seconds\n'
    root = _tree(tmp_path, {"order/a.py": body})

    code, _, err = _run(root, _exemptions(tmp_path, []), capsys)

    assert code == 1
    assert "order/a.py:3: price: float = 0.0" in err
    assert "a.py:4" not in err


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_windows_and_old_mac_line_endings_keep_the_line_numbers_and_markers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], newline: str
) -> None:
    lines = ["x = 1", "price: float = 0.0", "ttl: float = 1.0  # precision-ok: seconds", ""]
    root = _tree(tmp_path)
    (root / PKG / "order" / "a.py").write_bytes(newline.join(lines).encode("utf-8"))

    code, _, err = _run(root, _exemptions(tmp_path, []), capsys)

    assert code == 1
    assert "order/a.py:2: price: float = 0.0" in err
    assert "a.py:3" not in err


def test_a_byte_order_mark_does_not_change_what_the_first_line_matches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path)
    (root / PKG / "order" / "a.py").write_bytes(b"\xef\xbb\xbfttl: float = 5.0  # seconds\n")

    code, out, _ = _run(root, _exemptions(tmp_path, [_entry("order/a.py", "ttl: float = 5.0")]), capsys)

    assert code == 0
    assert "not-money=1" in out


def test_a_marker_inside_a_string_literal_is_not_a_marker(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, {"order/a.py": 'x: float = float("# precision-ok: fake")\n'})

    code, _, err = _run(root, _exemptions(tmp_path, []), capsys)

    assert code == 1
    assert "order/a.py:1" in err


# --- the exemption list ----------------------------------------------------------------------------------------------


def test_a_listed_line_passes_and_the_comment_is_not_part_of_the_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, {"order/a.py": "ttl: float = 5.0  # seconds\n"})

    code, out, _ = _run(root, _exemptions(tmp_path, [_entry("order/a.py", "ttl: float = 5.0")]), capsys)

    assert code == 0
    assert "not-money=1" in out


def test_an_exemption_covers_only_its_exact_code_text(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, {"order/a.py": "ttl: float = 5.0\nprice: float = 5.0\n"})

    code, _, err = _run(root, _exemptions(tmp_path, [_entry("order/a.py", "ttl: float = 5.0")]), capsys)

    assert code == 1
    assert "order/a.py:2: price: float = 5.0" in err
    assert "a.py:1" not in err


def test_an_exemption_for_one_file_does_not_cover_the_same_text_in_another(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, {"order/a.py": "ttl: float = 5.0\n", "risk/b.py": "ttl: float = 5.0\n"})

    code, _, err = _run(root, _exemptions(tmp_path, [_entry("order/a.py", "ttl: float = 5.0")]), capsys)

    assert code == 1
    assert "risk/b.py:1" in err


def test_a_stale_exemption_fails_the_gate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, {"order/a.py": "x = 1\n"})

    code, _, err = _run(root, _exemptions(tmp_path, [_entry("order/a.py", "ttl: float = 5.0")]), capsys)

    assert code == 1
    assert "matches 0 line(s), expects 1" in err


def test_an_exemption_count_is_a_limit_for_identical_lines(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, {"order/a.py": "ttl: float = 5.0\nttl: float = 5.0\n"})

    code, _, err = _run(root, _exemptions(tmp_path, [_entry("order/a.py", "ttl: float = 5.0")]), capsys)

    assert code == 1
    assert "order/a.py:2" in err and "covers 1 line(s)" in err


def test_an_exemption_count_above_the_actual_number_of_lines_is_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, {"order/a.py": "ttl: float = 5.0\n"})

    code, _, err = _run(root, _exemptions(tmp_path, [_entry("order/a.py", "ttl: float = 5.0", count=2)]), capsys)

    assert code == 1
    assert "matches 1 line(s), expects 2" in err


def test_an_exemption_count_matching_the_lines_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, {"order/a.py": "ttl: float = 5.0\nttl: float = 5.0\n"})

    code, out, _ = _run(root, _exemptions(tmp_path, [_entry("order/a.py", "ttl: float = 5.0", count=2)]), capsys)

    assert code == 0
    assert "not-money=2" in out


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"kind": "whatever"}, "kind must be one of"),
        ({"kind": "known-violation"}, "needs tracked_by"),
        ({"kind": "boundary-conversion"}, "needs tracked_by"),
        ({"kind": "known-violation", "tracked_by": "  "}, "needs tracked_by"),
        ({"reason": ""}, "reason must not be empty"),
        ({"reason": "   "}, "reason must not be empty"),
        ({"count": 0}, "count must be a positive integer"),
        ({"count": True}, "count must be a positive integer"),
        ({"count": "2"}, "count must be a positive integer"),
    ],
)
def test_a_malformed_exemption_entry_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: dict, message: str
) -> None:
    root = _tree(tmp_path, {"order/a.py": "ttl: float = 5.0\n"})

    code, _, err = _run(root, _exemptions(tmp_path, [_entry("order/a.py", "ttl: float = 5.0", **extra)]), capsys)

    assert code == 1
    assert message in err


def test_a_tracked_violation_with_an_owner_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, {"order/a.py": "mid: float = 5.0\n"})
    entry = _entry("order/a.py", "mid: float = 5.0", kind="known-violation", tracked_by="H03")

    code, out, _ = _run(root, _exemptions(tmp_path, [entry]), capsys)

    assert code == 0
    assert "known-violation=1" in out


def test_an_exemption_entry_that_is_not_a_mapping_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, {"order/a.py": "ttl: float = 5.0\n"})
    entries = ["ttl: float = 5.0", _entry("order/a.py", "ttl: float = 5.0")]

    code, _, err = _run(root, _exemptions(tmp_path, entries), capsys)

    assert code == 1
    assert "entry 1: not a mapping" in err


def test_the_same_line_listed_twice_is_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path, {"order/a.py": "ttl: float = 5.0\n"})
    entry = _entry("order/a.py", "ttl: float = 5.0")

    code, _, err = _run(root, _exemptions(tmp_path, [entry, dict(entry)]), capsys)

    assert code == 1
    assert "listed twice" in err


# --- unusable input exits 2, never 0 ---------------------------------------------------------------------------------


def test_a_missing_exemption_file_is_an_input_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path)

    code, _, err = _run(root, tmp_path / "nope.yaml", capsys)

    assert code == 2
    assert "cannot read exemptions" in err


@pytest.mark.parametrize("body", ["exemptions: [unclosed", "- a\n- b\n", "exemptions: 3\n"])
def test_an_unparseable_or_misshapen_exemption_file_is_an_input_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], body: str
) -> None:
    root = _tree(tmp_path)
    path = tmp_path / "exemptions.yaml"
    path.write_text(body, encoding="utf-8")

    code, _, err = _run(root, path, capsys)

    assert code == 2
    assert "float-gate:" in err


def test_a_missing_scan_target_is_an_input_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _tree(tmp_path)
    (root / PKG / "feed_adapter" / "normalizer.py").unlink()

    code, _, err = _run(root, _exemptions(tmp_path, []), capsys)

    assert code == 2
    assert "scan target does not exist" in err


def test_a_python_file_that_cannot_be_tokenized_is_an_input_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, {"order/broken.py": "x = (\n"})

    code, _, err = _run(root, _exemptions(tmp_path, []), capsys)

    assert code == 2
    assert "cannot tokenize" in err


# --- the real repository and its wiring ------------------------------------------------------------------------------


def test_the_repository_passes_its_own_float_gate(capsys: pytest.CaptureFixture[str]) -> None:
    code = gate.main([])
    out = capsys.readouterr()

    assert code == 0, out.err
    assert "float-gate: OK" in out.out


def test_every_committed_exemption_names_a_reason_and_a_known_kind() -> None:
    table, errors = gate.load_exemptions(gate.DEFAULT_EXEMPTIONS)

    assert errors == []
    assert table  # an empty list would mean the gate scans nothing it is told about
    assert {entry["kind"] for entry in table.values()} <= gate.KINDS


def test_ci_runs_the_gate_instead_of_a_grep_pipeline() -> None:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    float_steps = [step for step in steps if step.get("name") == "Detect float in financial paths"]

    assert len(float_steps) == 1
    assert float_steps[0]["run"].strip() == "make float-gate"


def test_make_check_includes_the_float_gate() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    check_line = next(line for line in makefile.splitlines() if line.startswith("check:"))

    assert "float-gate" in check_line.split("##")[0].split()
    assert "scripts/check_float_in_financial_paths.py" in makefile
