from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    feature_id: str
    dtype: str  # "i64" | "f64" | etc.
    scale: int = 0
    warmup_min_events: int = 1
    source_kind: str = "book"
    flags: int = 0


@dataclass(frozen=True, slots=True)
class FeatureSet:
    feature_set_id: str
    schema_version: int
    features: tuple[FeatureSpec, ...]

    @property
    def feature_ids(self) -> tuple[str, ...]:
        return tuple(spec.feature_id for spec in self.features)

    @property
    def index_by_id(self) -> dict[str, int]:
        return {spec.feature_id: idx for idx, spec in enumerate(self.features)}


class FeatureRegistry:
    """Versioned feature-set registry for runtime/research compatibility."""

    __slots__ = ("_sets", "_default_id")

    def __init__(self) -> None:
        self._sets: Dict[str, FeatureSet] = {}
        self._default_id: str | None = None

    def register(self, feature_set: FeatureSet, *, make_default: bool = False) -> None:
        self._sets[feature_set.feature_set_id] = feature_set
        if make_default or self._default_id is None:
            self._default_id = feature_set.feature_set_id

    def get(self, feature_set_id: str) -> FeatureSet:
        try:
            return self._sets[str(feature_set_id)]
        except KeyError as exc:
            raise KeyError(f"Unknown feature_set_id: {feature_set_id}") from exc

    def get_default(self) -> FeatureSet:
        if self._default_id is None:
            raise RuntimeError("FeatureRegistry has no registered feature sets")
        return self._sets[self._default_id]

    def set_default(self, feature_set_id: str) -> None:
        if feature_set_id not in self._sets:
            raise KeyError(f"Unknown feature_set_id: {feature_set_id}")
        self._default_id = feature_set_id

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._sets))

    def to_dict(self) -> dict[str, object]:
        return {
            "default": self._default_id,
            "feature_sets": {
                fsid: {
                    "schema_version": fs.schema_version,
                    "features": [
                        {
                            "feature_id": spec.feature_id,
                            "dtype": spec.dtype,
                            "scale": spec.scale,
                            "warmup_min_events": spec.warmup_min_events,
                            "source_kind": spec.source_kind,
                            "flags": spec.flags,
                        }
                        for spec in fs.features
                    ],
                }
                for fsid, fs in self._sets.items()
            },
        }

    @classmethod
    def from_sets(
        cls,
        feature_sets: Iterable[FeatureSet],
        *,
        default_id: str | None = None,
    ) -> "FeatureRegistry":
        reg = cls()
        for fs in feature_sets:
            reg.register(fs, make_default=(default_id is None and reg._default_id is None))
        if default_id:
            reg.set_default(default_id)
        return reg


def build_default_lob_feature_set_v1() -> FeatureSet:
    """Default shared LOB-derived feature set for the initial FeatureEngine prototype."""
    return FeatureSet(
        feature_set_id="lob_shared_v1",
        schema_version=1,
        features=(
            FeatureSpec("best_bid", "i64", scale=10_000, source_kind="book"),
            FeatureSpec("best_ask", "i64", scale=10_000, source_kind="book"),
            FeatureSpec("mid_price_x2", "i64", scale=10_000, source_kind="book"),
            FeatureSpec("spread_scaled", "i64", scale=10_000, source_kind="book"),
            FeatureSpec("bid_depth", "i64", source_kind="book"),
            FeatureSpec("ask_depth", "i64", source_kind="book"),
            # ratio in parts-per-million to keep integer semantics in v1
            FeatureSpec("depth_imbalance_ppm", "i64", scale=1_000_000, source_kind="book"),
            # microprice*2 in scaled price units (rounded int)
            FeatureSpec("microprice_x2", "i64", scale=10_000, source_kind="book"),
            # L1 queue quantities (preferred for microstructure deltas/OFI)
            FeatureSpec("l1_bid_qty", "i64", source_kind="book"),
            FeatureSpec("l1_ask_qty", "i64", source_kind="book"),
            FeatureSpec("l1_imbalance_ppm", "i64", scale=1_000_000, source_kind="book"),
            # OFI-style bounded-state features
            FeatureSpec("ofi_l1_raw", "i64", source_kind="book", warmup_min_events=2),
            FeatureSpec("ofi_l1_cum", "i64", source_kind="book", warmup_min_events=2),
            FeatureSpec("ofi_l1_ema8", "i64", source_kind="book", warmup_min_events=2),
            # Rolling-like bounded-state filters (EMA proxies)
            FeatureSpec("spread_ema8_scaled", "i64", scale=10_000, source_kind="book", warmup_min_events=2),
            FeatureSpec("depth_imbalance_ema8_ppm", "i64", scale=1_000_000, source_kind="book", warmup_min_events=2),
        ),
    )


def feature_id_to_index(feature_set: FeatureSet, feature_id: str) -> int:
    """Return the integer index for *feature_id* in *feature_set*.

    Raises ``KeyError`` if the feature is not found.
    """
    idx = feature_set.index_by_id.get(str(feature_id))
    if idx is None:
        raise KeyError(f"Feature '{feature_id}' not found in feature_set '{feature_set.feature_set_id}'")
    return idx


def default_feature_registry() -> FeatureRegistry:
    reg = FeatureRegistry()
    reg.register(build_default_lob_feature_set_v1(), make_default=True)
    return reg
