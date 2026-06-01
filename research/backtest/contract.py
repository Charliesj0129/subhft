"""Unified backtest contract spec (Stage 4 of the research-workflow consolidation).

Before this module existed, the three backtest engines —
:class:`research.backtest.maker_engine.MakerEngine`,
:class:`research.backtest.hft_native_runner.HftNativeRunner`, and
:class:`hft_platform.backtest.adapter.HftBacktestAdapter` — each accepted a
different shape of cost / fill / latency configuration: object-typed for
``MakerEngine``, string-named for ``HftNativeRunner``, and a 22-kwarg
constructor for ``HftBacktestAdapter``.

``BacktestContractSpec`` is the single declarative object new code should use
to configure any of those engines.  It does NOT replace the existing
constructors (those are load-bearing across hundreds of tests), but it does
ensure every spec-driven backtest reads from the same authoritative sources:

* cost: ``config/research/cost_profiles.yaml`` via
  :func:`research.backtest.cost_models.load_cost_profile`,
* latency: ``config/research/latency_profiles.yaml`` via
  :func:`hft_platform.alpha.latency_profiles.resolve_profile`,
* fill / queue / exchange models: hftbacktest-native string identifiers
  matched against ``research.backtest.types.BacktestConfig`` defaults.

A spec is intentionally a *value object*: frozen, hashable when its members
are hashable, and free of behaviour beyond resolution helpers.

See ``docs/runbooks/backtest-engine-selection.md`` for the engine-selection
matrix (CK-direct vs hftbacktest-native vs ResearchBacktestAdapter) and the
documented 14× pessimism / 577× optimism bias profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from research.backtest.cost_models import TAIFEXCost, load_cost_profile

if TYPE_CHECKING:  # avoid heavy imports during dataclass module load
    from hft_platform.alpha._validation_profile import ValidationProfile
    from research.registry.schemas import AlphaManifest


# Canonical defaults — mirror BacktestConfig + latency baseline so a spec
# built from an empty manifest is still runnable.
DEFAULT_QUEUE_MODEL = "PowerProbQueueModel(3.0)"
DEFAULT_LATENCY_MODEL = "IntpOrderLatency"
DEFAULT_EXCHANGE_MODEL = "NoPartialFillExchange"
DEFAULT_LATENCY_PROFILE_ID = "sim_p95_v2026-02-26"


@dataclass(frozen=True)
class BacktestContractSpec:
    """Declarative cost + fill + latency contract for any backtest engine.

    Required field is ``instrument``; every other field has a sensible default
    so a spec can be constructed incrementally.  Call
    :meth:`resolved_cost_model` or :meth:`resolved_latency_profile_dict` to
    pull the authoritative values from disk at use-time (never cached on the
    spec — the spec is a key, not a payload).
    """

    instrument: str
    cost_profile_ref: str = ""
    # hftbacktest-native string-typed models (HftNativeRunner / HftBacktestAdapter).
    fill_model_name: str = "QueueDepletionFill"
    queue_model_name: str = DEFAULT_QUEUE_MODEL
    exchange_model_name: str = DEFAULT_EXCHANGE_MODEL
    # hftbacktest-native engine model identifier (e.g. "IntpOrderLatency",
    # "ConstantLatency").  Distinct from ``latency_profile_id`` which names a
    # *profile* of P50/P95 numbers; ``latency_model_name`` is the *model class*
    # the engine instantiates to apply those numbers.
    latency_model_name: str = DEFAULT_LATENCY_MODEL
    # Latency profile id — resolves via hft_platform.alpha.latency_profiles to
    # the YAML row carrying submit/cancel ack ms.  NOT a model name.
    latency_profile_id: str = DEFAULT_LATENCY_PROFILE_ID
    # When non-zero, override the latency-profile-derived value (the engines
    # that take raw µs constructors use these directly).
    place_latency_us: int = 0
    modify_latency_us: int = 0
    cancel_latency_us: int = 0
    price_scale: int = 1_000_000
    # Optional pass-through metadata.
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    @classmethod
    def from_manifest(
        cls,
        manifest: "AlphaManifest",
        *,
        profile: "ValidationProfile | None" = None,
        instrument_override: str | None = None,
    ) -> "BacktestContractSpec":
        """Build a spec from an alpha manifest (and optional validation profile).

        The first entry of ``manifest.cost_profile_refs`` is used as the
        canonical cost profile.  When the manifest names multiple instruments
        (e.g. pair trades), pass ``instrument_override`` and the matching
        entry will be picked.  When the validation profile carries pipeline
        overrides (Stage 2), the latency profile id is taken from there.
        """
        refs = tuple(manifest.cost_profile_refs)
        instrument = instrument_override or manifest.instrument or (refs[0] if refs else "")
        cost_ref = ""
        if instrument_override and instrument_override in refs:
            cost_ref = instrument_override
        elif refs:
            cost_ref = refs[0]
        elif instrument:
            cost_ref = instrument

        latency_id: str = DEFAULT_LATENCY_PROFILE_ID
        place_us = modify_us = cancel_us = 0
        if profile is not None:
            overrides = profile.pipeline_overrides or {}
            latency_id = str(overrides.get("latency_profile_id", latency_id))
            place_us = int(overrides.get("local_decision_pipeline_latency_us", 0))
        if manifest.latency_profile:
            latency_id = manifest.latency_profile

        return cls(
            instrument=instrument,
            cost_profile_ref=cost_ref,
            latency_profile_id=latency_id,
            place_latency_us=place_us,
            modify_latency_us=modify_us,
            cancel_latency_us=cancel_us,
        )

    # ------------------------------------------------------------------
    # Resolution helpers (read-through; never cached)
    # ------------------------------------------------------------------

    def resolved_cost_model(self) -> TAIFEXCost:
        """Load the authoritative cost model from ``cost_profiles.yaml``."""
        if not self.cost_profile_ref:
            raise ValueError(
                f"BacktestContractSpec(instrument={self.instrument!r}) has no "
                "cost_profile_ref; set it explicitly or build via from_manifest."
            )
        return load_cost_profile(self.cost_profile_ref)

    def resolved_latency_profile_dict(self) -> dict[str, Any]:
        """Load the authoritative latency profile dict (lazy import)."""
        from hft_platform.alpha.latency_profiles import resolve_profile

        return resolve_profile(self.latency_profile_id)

    # ------------------------------------------------------------------
    # Engine adapters — return ready-to-use kwargs for each constructor.
    # Engines remain free to be constructed without a spec; these helpers
    # are syntactic sugar for spec-driven callers.
    # ------------------------------------------------------------------

    def maker_engine_kwargs(self) -> dict[str, Any]:
        """Kwargs for :class:`research.backtest.maker_engine.MakerEngine`."""
        from research.backtest.fill_models import QueueDepletionFill
        from research.backtest.maker_engine import LatencyProfile

        latency_profile: LatencyProfile | None = None
        if self.place_latency_us or self.cancel_latency_us:
            latency_profile = LatencyProfile(
                place_ns=self.place_latency_us * 1000,
                cancel_ns=self.cancel_latency_us * 1000,
            )
        return {
            "fill_model": QueueDepletionFill(),
            "cost_model": self.resolved_cost_model(),
            "latency_profile": latency_profile,
        }

    def hft_native_runner_kwargs(self) -> dict[str, Any]:
        """Kwargs forwarded into :class:`research.backtest.types.BacktestConfig`.

        ``latency_model`` is the hftbacktest model identifier (an engine class
        name); ``latency_profile_id`` is metadata identifying which YAML profile
        of P50/P95 numbers the run is calibrated against.  Earlier revisions
        mistakenly emitted the profile id where the model name belonged, which
        either crashed the adapter or silently fell back to ``ConstantLatency``
        — confirmed by the post-merge punch list (2026-05-29).
        """
        return {
            "queue_model": self.queue_model_name,
            "latency_model": self.latency_model_name,
            "latency_profile_id": self.latency_profile_id,
            "exchange_model": self.exchange_model_name,
        }

    def hft_backtest_adapter_kwargs(self) -> dict[str, Any]:
        """Kwargs forwarded into :class:`hft_platform.backtest.adapter.HftBacktestAdapter`."""
        # HftBacktestAdapter takes raw µs latencies — derive from latency profile
        # if the spec didn't already carry explicit values.
        place_us = self.place_latency_us
        modify_us = self.modify_latency_us
        cancel_us = self.cancel_latency_us
        if not place_us:
            try:
                lat = self.resolved_latency_profile_dict()
                # submit_ack_latency_ms is the standard place-side gate.
                place_us = int(float(lat["submit_ack_latency_ms"]) * 1000)
                modify_us = modify_us or int(float(lat.get("modify_ack_latency_ms", 0)) * 1000)
                cancel_us = cancel_us or int(float(lat.get("cancel_ack_latency_ms", 0)) * 1000)
            except (KeyError, ValueError, FileNotFoundError):
                # Latency YAML not on disk (test fixture) — fall back to a small constant.
                place_us = place_us or 100
        return {
            "latency_us": place_us,
            "modify_latency_us": modify_us,
            "cancel_latency_us": cancel_us,
            "queue_model": self.queue_model_name,
            "exchange_model": self.exchange_model_name,
            "price_scale": self.price_scale,
        }
