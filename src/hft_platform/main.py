import asyncio
import os
import signal

from structlog import get_logger

from hft_platform.config.loader import load_settings
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.observability.metrics_server import start_resilient_metrics_server
from hft_platform.services.system import HFTSystem

# Configure structlog globally? HFTSystem does it.
logger = get_logger("launcher")


_NON_SIM_ORDER_MODES = {"shadow", "live"}


def _refuse_non_sim_without_loop(settings: dict) -> None:
    """Block Docker-path startup if a non-sim engine has no loop binding.

    The bare ``python -m hft_platform.main`` entrypoint historically called
    ``HFTSystem()`` with no settings, which silently fell back to defaults.
    Loop_v1 demands a single source of truth: any production-shaped run
    (engine role + shadow/live order mode) MUST carry a loop_id.
    """
    runtime_role = str(os.getenv("HFT_RUNTIME_ROLE", "engine")).strip().lower().replace("-", "_")
    order_mode = str(os.getenv("HFT_ORDER_MODE", "sim")).strip().lower()
    if runtime_role != "engine":
        return
    if order_mode not in _NON_SIM_ORDER_MODES:
        return
    if not settings.get("loop_id"):
        raise RuntimeError(
            "loop_id required for non-sim engine startup "
            f"(runtime_role={runtime_role}, order_mode={order_mode}). "
            "Set HFT_LOOP=<id> or add loop_id to config/base/main.yaml."
        )


async def main():
    # Ensure MetricsRegistry is fully constructed BEFORE the Prometheus
    # scrape thread starts, preventing race conditions where a scrape
    # sees a partially-populated REGISTRY during __init__ re-registration.
    MetricsRegistry.get()

    prom_port_raw = os.getenv("HFT_PROM_PORT", "9090")
    try:
        prom_port = int(prom_port_raw)
    except ValueError:
        prom_port = 9090
    prom_addr = os.getenv("HFT_PROM_ADDR", "0.0.0.0")  # nosec B104
    start_resilient_metrics_server(prom_port, addr=prom_addr)
    logger.info("Prometheus metrics started", port=prom_port, addr=prom_addr)

    # Loop_v1: route Docker entrypoint through the CLI's loader so the
    # `loop_id` binding (and strict-mode validation) is honored. Without
    # this, `python -m hft_platform.main` bypassed loop binding entirely.
    settings, _ = load_settings()
    _refuse_non_sim_without_loop(settings)
    system = HFTSystem(settings)

    main_logger = get_logger("main")

    # Setup signal handlers
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def signal_handler(sig):
        main_logger.info("Signal received, stopping...", signal=sig)
        system.stop()
        stop_event.set()

    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, lambda s=s: signal_handler(s))

    main_logger.info("Launching HFT Platform...")

    system_task = asyncio.create_task(system.run())

    try:
        # Wait for stop signal or system crash
        done, pending = await asyncio.wait(
            [system_task, asyncio.create_task(stop_event.wait())], return_when=asyncio.FIRST_COMPLETED
        )

        # If system_task finished first (Validation error or crash)
        if system_task in done:
            exc = system_task.exception()
            if exc:
                logger.error("System crashed", error=str(exc))
                raise exc

    except Exception as e:
        main_logger.error("Launcher error", error=str(e))
    finally:
        system.stop()
        if not system_task.done():
            system_task.cancel()

        # P1 fix: wait for the detached stop_async() task so recorder drain,
        # final position checkpoint, and in-flight order cancellation actually
        # complete before the event loop is torn down. Without this, the
        # signal-handler path (SIGTERM/SIGINT) was fire-and-forget and could
        # leave the position checkpoint in a partial state, or drop buffered
        # audit rows that still needed to be flushed.
        stop_task = getattr(system, "_stop_async_task", None)
        tasks_to_await: list[asyncio.Task] = [system_task]
        if stop_task is not None and not stop_task.done():
            tasks_to_await.append(stop_task)
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks_to_await, return_exceptions=True),
                timeout=float(os.getenv("HFT_SHUTDOWN_GRACE_TIMEOUT_S", "90")),
            )
        except asyncio.TimeoutError:
            main_logger.warning("stop_async_timeout_on_shutdown")
        main_logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
