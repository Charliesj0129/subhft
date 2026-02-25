from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from hft_platform.events import FeatureUpdateEvent


@dataclass(slots=True)
class TypedFeatureFrameV1:
    """Prototype typed boundary frame for Python/Rust feature-plane transport.

    v1 encodes mixed numeric values using:
    - `value_kind_mask` bit i = 1 => values_f64[i] slot used, else values_i64[i]
    - both tuples are schema-ordered and same length as `feature_ids`
    """

    marker: str
    symbol: str
    seq: int
    source_ts_ns: int
    local_ts_ns: int
    feature_set_id: str
    schema_version: int
    changed_mask: int
    warmup_ready_mask: int
    quality_flags: int
    feature_ids: tuple[str, ...]
    value_kind_mask: int
    values_i64: tuple[int, ...]
    values_f64: tuple[float, ...]

    def value_at(self, idx: int) -> int | float:
        if idx < 0 or idx >= len(self.feature_ids):
            raise IndexError(idx)
        if (self.value_kind_mask >> idx) & 1:
            return float(self.values_f64[idx])
        return int(self.values_i64[idx])


def event_to_typed_frame(event: FeatureUpdateEvent) -> TypedFeatureFrameV1:
    values_i64: list[int] = []
    values_f64: list[float] = []
    kind_mask = 0
    for idx, v in enumerate(event.values):
        if isinstance(v, float):
            kind_mask |= 1 << idx
            values_i64.append(0)
            values_f64.append(float(v))
        else:
            values_i64.append(int(v))
            values_f64.append(0.0)
    return TypedFeatureFrameV1(
        marker="feature_update_v1",
        symbol=str(event.symbol),
        seq=int(event.seq),
        source_ts_ns=int(event.ts),
        local_ts_ns=int(event.local_ts),
        feature_set_id=str(event.feature_set_id),
        schema_version=int(event.schema_version),
        changed_mask=int(event.changed_mask),
        warmup_ready_mask=int(event.warmup_ready_mask),
        quality_flags=int(event.quality_flags),
        feature_ids=tuple(event.feature_ids),
        value_kind_mask=int(kind_mask),
        values_i64=tuple(values_i64),
        values_f64=tuple(values_f64),
    )


def typed_frame_to_event(frame: TypedFeatureFrameV1) -> FeatureUpdateEvent:
    values: list[int | float] = []
    for idx in range(len(frame.feature_ids)):
        if (int(frame.value_kind_mask) >> idx) & 1:
            values.append(float(frame.values_f64[idx]))
        else:
            values.append(int(frame.values_i64[idx]))
    return FeatureUpdateEvent(
        symbol=str(frame.symbol),
        ts=int(frame.source_ts_ns),
        local_ts=int(frame.local_ts_ns),
        seq=int(frame.seq),
        feature_set_id=str(frame.feature_set_id),
        schema_version=int(frame.schema_version),
        changed_mask=int(frame.changed_mask),
        warmup_ready_mask=int(frame.warmup_ready_mask),
        quality_flags=int(frame.quality_flags),
        feature_ids=tuple(frame.feature_ids),
        values=tuple(values),
    )


def typed_values_iter(frame: TypedFeatureFrameV1) -> Iterable[int | float]:
    for idx in range(len(frame.feature_ids)):
        yield frame.value_at(idx)
