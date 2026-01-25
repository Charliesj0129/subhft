import asyncio
import os
import signal

from prometheus_client import start_http_server
from structlog import get_logger

from hft_platform.services.system import HFTSystem

# Configure structlog globally? HFTSystem does it.
logger = get_logger("launcher")


async def main():
    prom_port_raw = os.getenv("HFT_PROM_PORT", "9090")
    try:
        prom_port = int(prom_port_raw)
    except ValueError:
        prom_port = 9090
    start_http_server(prom_port)
    logger.info("Prometheus metrics started", port=prom_port)

    # Load settings from file or env?
    # For now, minimal.
    system = HFTSystem()

    logger = get_logger("main")
    logger.info("HELLO FROM PATCHED MAIN - VERSION CHECK")

    # Setup signal handlers
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def signal_handler(sig):
        logger.info("Signal received, stopping...", signal=sig)
        system.stop()
        stop_event.set()

    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, lambda s=s: signal_handler(s))

    logger.info("Launching HFT Platform...")

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
        logger.error("Launcher error", error=str(e))
    finally:
        system.stop()
        if not system_task.done():
            system_task.cancel()
        await asyncio.gather(system_task, return_exceptions=True)
        logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
