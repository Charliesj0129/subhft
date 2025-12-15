import structlog
import logging
import sys

def configure_logging(level=logging.INFO):
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer()
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    # Basic standard logging capture
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)

def get_logger(name: str):
    return structlog.get_logger(name)
