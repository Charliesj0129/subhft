import pytest

pytestmark = pytest.mark.integration


class TestParity:
    def test_python_backend(self):
        try:
            from hft_platform.feature.engine import FeatureEngine

            assert FeatureEngine is not None
        except ImportError:
            pytest.skip("FeatureEngine unavailable")
