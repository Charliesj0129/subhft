"""Coverage tests for feature/boundary.py — missing lines 34-38, 47-49, 75, 94-95."""

from __future__ import annotations

import pytest

from hft_platform.events import FeatureUpdateEvent
from hft_platform.feature.boundary import (
    TypedFeatureFrameV1,
    event_to_typed_frame,
    typed_frame_to_event,
    typed_values_iter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_frame(
    feature_ids: tuple[str, ...] = ("spread_scaled", "ofi_l1_raw"),
    values_i64: tuple[int, ...] = (1000, 5),
    values_f64: tuple[float, ...] = (0.0, 0.0),
    value_kind_mask: int = 0,
) -> TypedFeatureFrameV1:
    return TypedFeatureFrameV1(
        marker="feature_update_v1",
        symbol="TXFD6",
        seq=1,
        source_ts_ns=1_000_000_000,
        local_ts_ns=1_000_000_001,
        feature_set_id="lob_shared_v3",
        schema_version=3,
        changed_mask=0b11,
        warmup_ready_mask=0b11,
        quality_flags=0,
        feature_ids=feature_ids,
        value_kind_mask=value_kind_mask,
        values_i64=values_i64,
        values_f64=values_f64,
    )


def _make_event(
    feature_ids: tuple[str, ...] = ("spread_scaled", "ofi_l1_raw"),
    values: tuple[int | float, ...] = (1000, 5),
) -> FeatureUpdateEvent:
    return FeatureUpdateEvent(
        symbol="TXFD6",
        ts=1_000_000_000,
        local_ts=1_000_000_001,
        seq=1,
        feature_set_id="lob_shared_v3",
        schema_version=3,
        changed_mask=0b11,
        warmup_ready_mask=0b11,
        quality_flags=0,
        feature_ids=feature_ids,
        values=values,
    )


# ---------------------------------------------------------------------------
# TypedFeatureFrameV1.value_at — lines 34-38
# ---------------------------------------------------------------------------


class TestTypedFeatureFrameValueAt:
    def test_value_at_negative_index_raises(self) -> None:
        frame = _make_frame()
        with pytest.raises(IndexError):
            frame.value_at(-1)

    def test_value_at_out_of_bounds_raises(self) -> None:
        frame = _make_frame()
        with pytest.raises(IndexError):
            frame.value_at(99)

    def test_value_at_out_of_bounds_exact_length_raises(self) -> None:
        frame = _make_frame()
        with pytest.raises(IndexError):
            frame.value_at(len(frame.feature_ids))

    def test_value_at_integer_slot_returns_int(self) -> None:
        # kind_mask = 0 → all i64 slots
        frame = _make_frame(value_kind_mask=0)
        result = frame.value_at(0)
        assert isinstance(result, int)
        assert result == 1000

    def test_value_at_float_slot_returns_float(self) -> None:
        # kind_mask bit 0 set → index 0 uses f64 slot
        frame = _make_frame(
            feature_ids=("imbalance",),
            values_i64=(0,),
            values_f64=(0.75,),
            value_kind_mask=0b1,
        )
        result = frame.value_at(0)
        assert isinstance(result, float)
        assert result == pytest.approx(0.75)

    def test_value_at_mixed_kinds(self) -> None:
        # bit 1 set → index 1 is float, index 0 is int
        frame = _make_frame(
            feature_ids=("spread_scaled", "imbalance"),
            values_i64=(500, 0),
            values_f64=(0.0, 0.33),
            value_kind_mask=0b10,
        )
        assert isinstance(frame.value_at(0), int)
        assert frame.value_at(0) == 500
        assert isinstance(frame.value_at(1), float)
        assert frame.value_at(1) == pytest.approx(0.33)


# ---------------------------------------------------------------------------
# event_to_typed_frame — float branch, lines 47-49
# ---------------------------------------------------------------------------


class TestEventToTypedFrame:
    def test_integer_values_produce_zero_kind_mask(self) -> None:
        event = _make_event(values=(1000, 5))
        frame = event_to_typed_frame(event)
        assert frame.value_kind_mask == 0
        assert frame.values_i64 == (1000, 5)
        assert frame.values_f64 == (0.0, 0.0)

    def test_float_value_sets_kind_mask_bit(self) -> None:
        # value at index 1 is a float → bit 1 should be set
        event = _make_event(
            feature_ids=("spread_scaled", "imbalance"),
            values=(1000, 0.5),
        )
        frame = event_to_typed_frame(event)
        assert frame.value_kind_mask == 0b10  # bit 1 set
        assert frame.values_i64[1] == 0  # placeholder
        assert frame.values_f64[1] == pytest.approx(0.5)
        assert frame.values_i64[0] == 1000
        assert frame.values_f64[0] == 0.0

    def test_all_float_values_sets_all_bits(self) -> None:
        event = _make_event(
            feature_ids=("a", "b"),
            values=(0.25, 0.75),
        )
        frame = event_to_typed_frame(event)
        assert frame.value_kind_mask == 0b11
        assert frame.values_f64[0] == pytest.approx(0.25)
        assert frame.values_f64[1] == pytest.approx(0.75)

    def test_symbol_and_metadata_preserved(self) -> None:
        event = _make_event()
        frame = event_to_typed_frame(event)
        assert frame.symbol == "TXFD6"
        assert frame.feature_set_id == "lob_shared_v3"
        assert frame.schema_version == 3
        assert frame.seq == 1
        assert frame.marker == "feature_update_v1"


# ---------------------------------------------------------------------------
# typed_frame_to_event — float branch, line 75
# ---------------------------------------------------------------------------


class TestTypedFrameToEvent:
    def test_integer_values_roundtrip(self) -> None:
        frame = _make_frame(value_kind_mask=0)
        event = typed_frame_to_event(frame)
        assert event.symbol == "TXFD6"
        assert event.values == (1000, 5)
        assert all(isinstance(v, int) for v in event.values)

    def test_float_values_roundtrip(self) -> None:
        # bit 0 set → index 0 should come from f64
        frame = _make_frame(
            feature_ids=("imbalance",),
            values_i64=(0,),
            values_f64=(0.42,),
            value_kind_mask=0b1,
        )
        event = typed_frame_to_event(frame)
        assert isinstance(event.values[0], float)
        assert event.values[0] == pytest.approx(0.42)

    def test_mixed_kinds_roundtrip(self) -> None:
        frame = _make_frame(
            feature_ids=("spread_scaled", "imbalance"),
            values_i64=(800, 0),
            values_f64=(0.0, 0.99),
            value_kind_mask=0b10,
        )
        event = typed_frame_to_event(frame)
        assert isinstance(event.values[0], int)
        assert event.values[0] == 800
        assert isinstance(event.values[1], float)
        assert event.values[1] == pytest.approx(0.99)

    def test_full_event_to_frame_to_event_roundtrip(self) -> None:
        original = _make_event(
            feature_ids=("spread_scaled", "imbalance"),
            values=(500, 0.5),
        )
        frame = event_to_typed_frame(original)
        recovered = typed_frame_to_event(frame)
        assert recovered.symbol == original.symbol
        assert recovered.seq == original.seq
        assert recovered.feature_ids == original.feature_ids
        assert recovered.values[0] == original.values[0]
        assert recovered.values[1] == pytest.approx(original.values[1])


# ---------------------------------------------------------------------------
# typed_values_iter — lines 94-95
# ---------------------------------------------------------------------------


class TestTypedValuesIter:
    def test_yields_all_integer_values(self) -> None:
        frame = _make_frame(value_kind_mask=0)
        values = list(typed_values_iter(frame))
        assert values == [1000, 5]
        assert all(isinstance(v, int) for v in values)

    def test_yields_float_value_for_float_slot(self) -> None:
        frame = _make_frame(
            feature_ids=("imbalance",),
            values_i64=(0,),
            values_f64=(0.77,),
            value_kind_mask=0b1,
        )
        values = list(typed_values_iter(frame))
        assert len(values) == 1
        assert isinstance(values[0], float)
        assert values[0] == pytest.approx(0.77)

    def test_yields_correct_count(self) -> None:
        frame = _make_frame(
            feature_ids=("a", "b", "c"),
            values_i64=(1, 2, 3),
            values_f64=(0.0, 0.0, 0.0),
            value_kind_mask=0,
        )
        values = list(typed_values_iter(frame))
        assert len(values) == 3
        assert values == [1, 2, 3]

    def test_empty_feature_ids_yields_nothing(self) -> None:
        frame = _make_frame(
            feature_ids=(),
            values_i64=(),
            values_f64=(),
            value_kind_mask=0,
        )
        values = list(typed_values_iter(frame))
        assert values == []

    def test_iter_matches_value_at(self) -> None:
        frame = _make_frame(
            feature_ids=("spread_scaled", "imbalance"),
            values_i64=(300, 0),
            values_f64=(0.0, 0.12),
            value_kind_mask=0b10,
        )
        iter_values = list(typed_values_iter(frame))
        direct_values = [frame.value_at(i) for i in range(len(frame.feature_ids))]
        assert iter_values[0] == direct_values[0]
        assert iter_values[1] == pytest.approx(direct_values[1])
