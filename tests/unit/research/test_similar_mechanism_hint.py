"""R05: report-only hint that lists KILLED manifests with a mechanism like a new candidate's.

Drives ``scripts/similar_mechanism_hint.py`` on synthetic trees, so no test depends on the live manifests.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml

# scripts/ is not a Python package; load the module by file path.
_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "similar_mechanism_hint.py"
_spec = importlib.util.spec_from_file_location("similar_mechanism_hint", _SCRIPT)
assert _spec is not None and _spec.loader is not None
smh = importlib.util.module_from_spec(_spec)
sys.modules["similar_mechanism_hint"] = smh
_spec.loader.exec_module(smh)

VWAP = "Price deviation from session VWAP predicts mean reversion at 15min horizons"


def _manifest(path: Path, **fields) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(fields), encoding="utf-8")
    return path


def _active(root: Path, alpha_id: str, **fields) -> Path:
    return _manifest(root / "research" / "alphas" / alpha_id / "manifest.yaml", alpha_id=alpha_id, **fields)


def _archived(root: Path, alpha_id: str, **fields) -> Path:
    return _manifest(
        root / "research" / "archive" / "alphas_2026-04-17" / alpha_id / "manifest.yaml", alpha_id=alpha_id, **fields
    )


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _archived(
        root, "r37_vwap_reversion", status="KILLED", hypothesis=VWAP, instrument="TXFD6", kill_reason="IC inflated"
    )
    _archived(
        root,
        "r41_institutional_flow",
        status="KILLED",
        name="Institutional Flow Accumulation",
        signal="ofi_autocorr_30m",
        instrument="TXFD6",
    )
    _active(
        root, "new_vwap", status="DRAFT", hypothesis="VWAP deviation mean reversion on 5min bars", instrument="TMFD6"
    )
    return root


def _run(root: Path, *argv: str) -> int:
    return smh.main(["--root", str(root), *argv])


def _tree_hash(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _doc(**fields):
    return smh.doc_from_mapping(fields, path="p", location="active", fallback_id="x")


class TestTokenize:
    def test_tokenize_drops_stopwords_horizons_and_contract_codes(self):
        got = smh.tokenize("The signal on TXFD6 at 15min and 30m horizons: VWAP deviation, ofi_autocorr_30m")
        assert got == {"vwap", "deviation", "ofi", "autocorr"}

    def test_tokenize_drops_a_trailing_plural_s_but_not_ss_is_us(self):
        assert smh.tokenize("imbalances flows") == {"imbalance", "flow"}
        assert smh.tokenize("basis class analysis bonus") == {"basis", "analysis", "bonus"}

    def test_tokenize_of_empty_or_all_stopwords_is_empty(self):
        assert smh.tokenize("") == frozenset()
        assert smh.tokenize("the cost of a trade") == frozenset()


class TestRank:
    def test_hint_names_the_killed_manifest_with_the_shared_words(self, tmp_path):
        docs, _ = smh.scan(_tree(tmp_path))
        cand = next(d for d in docs if d.alpha_id == "new_vwap")
        hints = smh.rank(cand, [d for d in docs if smh.is_killed(d)], top=5, min_score=0.1, min_shared=2)
        assert [h.alpha_id for h in hints] == ["r37_vwap_reversion"]
        assert {"vwap", "deviation", "mean", "reversion"} <= set(hints[0].shared_tokens)
        assert hints[0].reason == "IC inflated"
        assert hints[0].path == "research/archive/alphas_2026-04-17/r37_vwap_reversion/manifest.yaml"

    def test_only_killed_manifests_are_hinted(self, tmp_path):
        root = _tree(tmp_path)
        for i, status in enumerate(("DEPRECATED", "PARKED", "PROTOTYPE", "DRAFT")):
            _active(root, f"twin_{i}", status=status, hypothesis=VWAP)
        docs, _ = smh.scan(root)
        cand = next(d for d in docs if d.alpha_id == "new_vwap")
        killed = [d for d in docs if smh.is_killed(d)]
        assert {d.alpha_id for d in killed} == {"r37_vwap_reversion", "r41_institutional_flow"}
        assert {h.alpha_id for h in smh.rank(cand, killed, top=9, min_score=0.0, min_shared=1)} <= {
            d.alpha_id for d in killed
        }

    @pytest.mark.parametrize("status", ["KILLED", "killed", "Killed"])
    def test_killed_is_recognised_in_any_letter_case(self, status):
        assert smh.is_killed(_doc(alpha_id="a", status=status))

    @pytest.mark.parametrize("status", [None, "DRAFT", "KILLED_MAYBE", "unkilled"])
    def test_other_statuses_are_not_killed(self, status):
        assert not smh.is_killed(_doc(alpha_id="a", status=status))

    def test_status_with_an_inline_comment_still_reads_as_killed(self, tmp_path):
        root = tmp_path / "repo"
        path = root / "research" / "archive" / "alphas_2026-05-28" / "c14" / "manifest.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(
            "alpha_id: c14\nstatus: KILLED  # 2026-04-18: cost-model error\nhypothesis: maker front month\n"
        )
        docs, skipped = smh.scan(root)
        assert skipped == [] and [smh.is_killed(d) for d in docs] == [True]

    def test_a_killed_candidate_is_never_hinted_against_itself(self, tmp_path):
        docs, _ = smh.scan(_tree(tmp_path))
        killed = [d for d in docs if smh.is_killed(d)]
        own = next(d for d in killed if d.alpha_id == "r37_vwap_reversion")
        assert "r37_vwap_reversion" not in {
            h.alpha_id for h in smh.rank(own, killed, top=9, min_score=0.0, min_shared=1)
        }

    def test_one_shared_word_is_not_a_hint_by_default(self):
        cand = _doc(alpha_id="c", hypothesis="vwap breakout")
        old = _doc(alpha_id="k", status="KILLED", hypothesis="vwap deviation fade")
        assert smh.rank(cand, [old], top=5, min_score=0.0, min_shared=2) == []
        assert [h.alpha_id for h in smh.rank(cand, [old], top=5, min_score=0.0, min_shared=1)] == ["k"]

    def test_score_is_the_jaccard_index_and_the_floor_filters_it(self):
        cand = _doc(alpha_id="c", hypothesis="kappa beta gamma delta")
        old = _doc(alpha_id="k", status="KILLED", hypothesis="kappa beta epsilon zeta")
        (hint,) = smh.rank(cand, [old], top=5, min_score=0.0, min_shared=2)
        assert hint.score == pytest.approx(2 / 6, abs=1e-3)
        assert smh.rank(cand, [old], top=5, min_score=0.34, min_shared=2) == []

    def test_kill_reason_is_shown_but_never_matched(self):
        cand = _doc(alpha_id="c", hypothesis="momentum breakout continuation")
        old = _doc(
            alpha_id="k",
            status="KILLED",
            hypothesis="queue position maker",
            kill_reason="momentum breakout continuation failed",
        )
        assert smh.rank(cand, [old], top=5, min_score=0.0, min_shared=1) == []
        assert old.reason == "momentum breakout continuation failed"

    def test_results_are_ordered_by_score_then_id_and_cut_at_top(self):
        cand = _doc(alpha_id="c", hypothesis="kappa beta gamma delta")
        mk = lambda i, text: _doc(alpha_id=i, status="KILLED", hypothesis=text)  # noqa: E731
        killed = [
            mk("b_twin", "kappa beta epsilon"),
            mk("a_twin", "kappa beta epsilon"),
            mk("closest", "kappa beta gamma"),
            mk("far", "kappa beta zeta eta theta"),
        ]
        hints = smh.rank(cand, killed, top=3, min_score=0.0, min_shared=2)
        assert [h.alpha_id for h in hints] == ["closest", "a_twin", "b_twin"]

    def test_instrument_is_compared_but_not_scored(self):
        cand = _doc(alpha_id="c", hypothesis="kappa beta", instrument="TMFD6")
        same = _doc(alpha_id="s", status="KILLED", hypothesis="kappa beta", instrument="TMFD6")
        other = _doc(alpha_id="o", status="KILLED", hypothesis="kappa beta", instrument="TXFD6")
        unset = _doc(alpha_id="u", status="KILLED", hypothesis="kappa beta")
        got = {h.alpha_id: h for h in smh.rank(cand, [same, other, unset], top=9, min_score=0.0, min_shared=2)}
        assert (got["s"].same_instrument, got["o"].same_instrument, got["u"].same_instrument) == (True, False, None)
        assert {h.score for h in got.values()} == {1.0}

    def test_shared_data_fields_are_listed_exactly(self):
        cand = _doc(alpha_id="c", hypothesis="kappa beta", data_fields=["bid_qty", "ask_qty", "mid"])
        old = _doc(alpha_id="k", status="KILLED", hypothesis="kappa beta", data_fields=["ask_qty", "mid", "vwap"])
        (hint,) = smh.rank(cand, [old], top=5, min_score=0.0, min_shared=2)
        assert hint.shared_data_fields == ("ask_qty", "mid")


class TestReason:
    def test_reason_collapses_whitespace_and_truncates(self):
        doc = _doc(alpha_id="k", kill_reason="line one\n  line two\n" + "x" * 400)
        assert doc.reason is not None
        assert doc.reason.startswith("line one line two xxx")
        assert len(doc.reason) == 200 and doc.reason.endswith("...")

    def test_reason_falls_back_to_correction_note_then_none(self):
        assert _doc(alpha_id="k", correction_note="cost model wrong").reason == "cost model wrong"
        assert _doc(alpha_id="k", kill_reason="", correction_note="  ").reason is None
        assert _doc(alpha_id="k").reason is None


class TestScan:
    def test_scan_reads_active_and_archive_and_names_the_unreadable(self, tmp_path):
        root = _tree(tmp_path)
        bad_yaml = root / "research" / "alphas" / "broken" / "manifest.yaml"
        bad_yaml.parent.mkdir(parents=True)
        bad_yaml.write_text("alpha_id: [unclosed\n", encoding="utf-8")
        listy = root / "research" / "alphas" / "listy" / "manifest.yaml"
        listy.parent.mkdir(parents=True)
        listy.write_text("- just\n- a list\n", encoding="utf-8")
        docs, skipped = smh.scan(root)
        assert {d.alpha_id for d in docs} == {"r37_vwap_reversion", "r41_institutional_flow", "new_vwap"}
        assert {d.location for d in docs} == {"active", "archive"}
        assert skipped == ["research/alphas/broken/manifest.yaml", "research/alphas/listy/manifest.yaml"]

    def test_manifest_without_alpha_id_takes_its_directory_name(self, tmp_path):
        root = tmp_path / "repo"
        _manifest(root / "research" / "alphas" / "dir_name" / "manifest.yaml", status="KILLED", hypothesis="kappa beta")
        (doc,) = smh.scan(root)[0]
        assert doc.alpha_id == "dir_name"

    def test_the_local_kill_ledger_is_not_a_source(self, tmp_path):
        root = _tree(tmp_path)
        ledger = root / "research" / "alphas" / "_kill_ledger.jsonl"
        ledger.write_text(json.dumps({"alpha_id": "ledger_only", "reason": "vwap deviation mean reversion"}) + "\n")
        docs, skipped = smh.scan(root)
        assert "ledger_only" not in {d.alpha_id for d in docs} and skipped == []


class TestMain:
    def test_alpha_id_report_lists_the_hint_and_the_disclaimer(self, tmp_path, capsys):
        assert _run(_tree(tmp_path), "--alpha-id", "new_vwap") == 0
        out = capsys.readouterr().out
        assert "candidate: new_vwap" in out and "scanned 3 manifests, 2 KILLED" in out
        assert "1. r37_vwap_reversion" in out and "instrument TXFD6" in out and "stated reason: IC inflated" in out
        assert smh.NOTE in out

    def test_manifest_path_is_accepted_for_a_candidate_outside_the_tree(self, tmp_path, capsys):
        draft = _manifest(tmp_path / "draft" / "manifest.yaml", alpha_id="draft_x", hypothesis=VWAP, instrument="TMFD6")
        assert _run(_tree(tmp_path), "--manifest", str(draft)) == 0
        out = capsys.readouterr().out
        assert "candidate: draft_x" in out and "r37_vwap_reversion" in out

    def test_ad_hoc_hypothesis_works_before_a_manifest_exists(self, tmp_path, capsys):
        root = _tree(tmp_path)
        assert _run(root, "--hypothesis", "VWAP deviation reversion", "--instrument", "TMFD6") == 0
        assert "r37_vwap_reversion" in capsys.readouterr().out

    def test_no_similar_manifest_is_a_normal_exit(self, tmp_path, capsys):
        assert _run(_tree(tmp_path), "--hypothesis", "gamma scalping straddle volatility") == 0
        assert "no KILLED manifest passes the similarity threshold" in capsys.readouterr().out

    def test_unreadable_manifests_are_listed_not_fatal(self, tmp_path, capsys):
        root = _tree(tmp_path)
        (root / "research" / "alphas" / "broken").mkdir()
        (root / "research" / "alphas" / "broken" / "manifest.yaml").write_text("a: [", encoding="utf-8")
        assert _run(root, "--alpha-id", "new_vwap") == 0
        assert "skipped (unreadable): research/alphas/broken/manifest.yaml" in capsys.readouterr().out

    def test_unknown_alpha_id_exits_2(self, tmp_path, capsys):
        assert _run(_tree(tmp_path), "--alpha-id", "nope") == 2
        assert "'nope' is in no manifest" in capsys.readouterr().err

    def test_unreadable_candidate_manifest_exits_2(self, tmp_path, capsys):
        assert _run(_tree(tmp_path), "--manifest", str(tmp_path / "missing.yaml")) == 2
        assert "cannot read candidate manifest" in capsys.readouterr().err
        not_map = tmp_path / "list.yaml"
        not_map.write_text("- a\n", encoding="utf-8")
        assert _run(_tree(tmp_path), "--manifest", str(not_map)) == 2

    @pytest.mark.parametrize(
        "argv", [["--top", "0"], ["--min-shared", "0"], ["--min-score", "1.5"], ["--min-score", "-0.1"]]
    )
    def test_bad_limits_exit_2(self, tmp_path, argv):
        assert _run(_tree(tmp_path), "--alpha-id", "new_vwap", *argv) == 2

    def test_a_candidate_is_required_and_only_one_is_allowed(self, tmp_path):
        with pytest.raises(SystemExit) as none:
            _run(_tree(tmp_path))
        with pytest.raises(SystemExit) as two:
            _run(_tree(tmp_path), "--alpha-id", "a", "--hypothesis", "b")
        assert (none.value.code, two.value.code) == (2, 2)

    @pytest.mark.parametrize("argv", [["--name", "x"], ["--data-fields", "a,b"], ["--instrument", "TMFD6"]])
    def test_ad_hoc_options_without_a_hypothesis_exit_2(self, tmp_path, capsys, argv):
        assert _run(_tree(tmp_path), "--alpha-id", "new_vwap", *argv) == 2
        assert "need --hypothesis" in capsys.readouterr().err

    def test_an_empty_alpha_id_is_not_read_as_an_ad_hoc_candidate(self, tmp_path, capsys):
        assert _run(_tree(tmp_path), "--alpha-id", "") == 2
        assert "is in no manifest" in capsys.readouterr().err

    def test_an_unwritable_json_out_exits_2(self, tmp_path, capsys):
        missing_dir = tmp_path / "no_such_dir" / "hint.json"
        assert _run(_tree(tmp_path), "--alpha-id", "new_vwap", "--json-out", str(missing_dir)) == 2
        assert "cannot write" in capsys.readouterr().err

    def test_json_out_carries_the_hints_and_the_note(self, tmp_path):
        out = tmp_path / "out" / "hint.json"
        out.parent.mkdir()
        assert _run(_tree(tmp_path), "--alpha-id", "new_vwap", "--json-out", str(out)) == 0
        body = json.loads(out.read_text(encoding="utf-8"))
        assert body["candidate"] == "new_vwap" and body["scanned"] == 3 and body["killed"] == 2
        assert body["note"] == smh.NOTE
        assert [h["alpha_id"] for h in body["hints"]] == ["r37_vwap_reversion"]
        assert "verdict" not in body and all("verdict" not in h for h in body["hints"])

    def test_a_run_changes_no_file_in_the_scanned_tree(self, tmp_path):
        root = _tree(tmp_path)
        before = _tree_hash(root)
        assert _run(root, "--alpha-id", "new_vwap", "--top", "9", "--min-score", "0") == 0
        assert _tree_hash(root) == before
        assert yaml.safe_load((root / "research/alphas/new_vwap/manifest.yaml").read_text())["status"] == "DRAFT"
