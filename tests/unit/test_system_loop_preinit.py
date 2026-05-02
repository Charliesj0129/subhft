"""H9: HFTSystem.loop must exist as a None-initialized attribute before
run() is scheduled, so early broker-thread callbacks can make a defensive
is-None check without tripping AttributeError.

Root cause: ``self.loop`` is only assigned inside ``async def run()``.
Between ``__init__`` (which may already have wired broker sessions via
``SystemBootstrapper.build()``) and the first tick of ``run()``, a
broker callback landing on a separate thread would see
``hasattr(self, "loop") == False``. Current defensive readers use
``hasattr(...)`` or ``getattr(..., None)``, so no crash today — but the
invariant is fragile and a single missed defensive check elsewhere would
drop the event silently.

Fix: initialize ``self.loop = None`` in ``__init__`` so the attribute
always exists; every subsequent reader can use ``if self.loop is not None``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_hftsystem_loop_attribute_exists_after_init():
    """After __init__ completes, self.loop must be an attribute (None)."""
    # Patch SystemBootstrapper.build so __init__ does not actually
    # construct the whole platform.
    with patch("hft_platform.services.system.SystemBootstrapper") as MockBootstrapper:
        registry = MagicMock()
        registry.raw_queue = MagicMock()
        registry.raw_exec_queue = MagicMock()
        registry.risk_queue = MagicMock()
        registry.order_queue = MagicMock()
        registry.recorder_queue = MagicMock()
        registry.gateway_service = None
        registry.intent_channel = None
        registry.checkpoint_writer = None
        registry.startup_verifier = None
        registry.startup_fill_reconciler = None
        registry.session_governor = None
        registry.autonomy_monitor = None
        registry.daily_report_service = None
        registry.position_stuck_monitor = None
        registry.evidence_writer = None
        registry.platform_degrade_controller = None
        registry.platform_degrade_inputs = None
        # attributes accessed in __init__
        md_service = MagicMock()
        md_service.register_on_reconnect = MagicMock()
        registry.md_service = md_service
        registry.order_adapter = MagicMock()
        registry.execution_gateway = MagicMock()
        registry.exec_service = MagicMock()
        registry.risk_engine = MagicMock()
        registry.recon_service = MagicMock()
        registry.strategy_runner = MagicMock()
        registry.recorder = MagicMock()
        registry.position_store = MagicMock()
        registry.order_id_map = MagicMock()
        registry.storm_guard = MagicMock()
        registry.md_client = MagicMock()
        registry.order_client = MagicMock()
        registry.client = MagicMock()
        registry.symbol_metadata = MagicMock()
        registry.price_scale_provider = MagicMock()
        registry.bus = MagicMock()
        instance = MockBootstrapper.return_value
        instance.build.return_value = registry
        instance.build_platform_degrade_inputs.return_value = MagicMock()

        from hft_platform.services.system import HFTSystem

        system = HFTSystem(settings={})
        # H9 invariant: self.loop must exist post-__init__.
        assert hasattr(system, "loop"), "self.loop must be set in __init__"
        assert system.loop is None, "self.loop must default to None until run()"
