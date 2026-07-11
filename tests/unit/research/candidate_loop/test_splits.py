"""split_v1 definition: structure, disjointness, NPZ path convention."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from research.candidate_loop.splits import (
    DaySymbol,
    load_split_definition,
    npz_path_for,
)

SPLIT_PATH = Path(__file__).resolve().parents[4] / "config" / "research" / "candidate_loop" / "split_definition_v1.yaml"


class TestRealSplitDefinition:
    def test_versions_and_counts(self) -> None:
        d = load_split_definition(SPLIT_PATH)
        assert d.split_definition_version == "split_v1"
        assert d.data_version == "txf_l2_2026H1_v1"
        assert len(d.splits["train"]) == 40
        assert len(d.splits["validation"]) == 14
        assert len(d.splits["test"]) == 13

    def test_splits_are_contiguous_in_time(self) -> None:
        d = load_split_definition(SPLIT_PATH)
        train_start, train_end = d.split_range("train")
        val_start, val_end = d.split_range("validation")
        test_start, test_end = d.split_range("test")
        assert train_start == "2026-01-26"
        assert train_end < val_start < val_end < test_start <= test_end

    def test_all_pairs_ordered_by_split(self) -> None:
        d = load_split_definition(SPLIT_PATH)
        names = [name for name, _ in d.all_pairs()]
        assert names == ["train"] * 40 + ["validation"] * 14 + ["test"] * 13


class TestValidation:
    def _write(self, tmp_path: Path, splits: dict) -> Path:
        path = tmp_path / "splits.yaml"
        path.write_text(yaml.safe_dump({"split_definition_version": "split_v1", "data_version": "v", "splits": splits}))
        return path

    def test_day_in_two_splits_rejected(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            {
                "train": [{"day": "2026-01-26", "symbol": "TXFB6"}],
                "validation": [{"day": "2026-01-26", "symbol": "TXFB6"}],
                "test": [{"day": "2026-01-28", "symbol": "TXFB6"}],
            },
        )
        with pytest.raises(ValueError, match="appears in both"):
            load_split_definition(path)

    def test_empty_split_rejected(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            {
                "train": [{"day": "2026-01-26", "symbol": "TXFB6"}],
                "validation": [],
                "test": [{"day": "2026-01-28", "symbol": "TXFB6"}],
            },
        )
        with pytest.raises(ValueError, match="empty"):
            load_split_definition(path)

    def test_unknown_split_name_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, {"holdout": [{"day": "2026-01-26", "symbol": "TXFB6"}]})
        with pytest.raises(ValueError, match="Unknown split"):
            load_split_definition(path)


class TestNpzPath:
    def test_inventory_convention(self) -> None:
        path = npz_path_for(Path("research/data/raw"), DaySymbol(day="2026-04-13", symbol="TXFD6"))
        assert path == Path("research/data/raw/txfd6/TXFD6_2026-04-13_l2.hftbt.npz")
