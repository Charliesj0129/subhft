import logging

from hft_platform.utils.logging import configure_logging, get_logger


def test_get_logger_returns_logger():
    logger = get_logger("unit-test")
    assert logger is not None


def test_configure_logging_sets_root_level():
    configure_logging(level=logging.DEBUG)
    assert isinstance(logging.getLogger().getEffectiveLevel(), int)
