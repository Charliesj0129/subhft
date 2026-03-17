from hft_platform.observability._label_sanitize import sanitize_exception_type


def test_known_types_pass_through():
    for exc_class in (ValueError, TypeError, KeyError, IndexError, RuntimeError, ConnectionError, TimeoutError):
        exc = exc_class("test")
        assert sanitize_exception_type(exc) == exc_class.__name__


def test_unknown_type_returns_other():
    class CustomBrokerError(Exception):
        pass

    exc = CustomBrokerError("something")
    assert sanitize_exception_type(exc) == "other"


def test_base_exception_subclass():
    class MyKeyboardInterrupt(KeyboardInterrupt):
        pass

    exc = MyKeyboardInterrupt()
    assert sanitize_exception_type(exc) == "other"
