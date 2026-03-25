"""Tests for live mode startup confirmation guard."""
import pytest


class TestLiveConfirmGuard:
    def test_live_mode_without_confirm_exits(self, monkeypatch):
        """HFT_ORDER_MODE=live without HFT_LIVE_CONFIRM -> SystemExit."""
        monkeypatch.setenv("HFT_ORDER_MODE", "live")
        monkeypatch.setenv("HFT_MODE", "live")
        monkeypatch.delenv("HFT_LIVE_CONFIRM", raising=False)
        from hft_platform.services.bootstrap import validate_order_mode_safety

        with pytest.raises(SystemExit):
            validate_order_mode_safety()

    def test_live_mode_with_confirm_passes(self, monkeypatch):
        """HFT_ORDER_MODE=live + HFT_LIVE_CONFIRM=yes-i-know -> passes."""
        monkeypatch.setenv("HFT_ORDER_MODE", "live")
        monkeypatch.setenv("HFT_MODE", "live")
        monkeypatch.setenv("HFT_LIVE_CONFIRM", "yes-i-know")
        from hft_platform.services.bootstrap import validate_order_mode_safety

        validate_order_mode_safety()  # should not raise

    def test_sim_mode_no_confirm_needed(self, monkeypatch):
        """HFT_ORDER_MODE=sim -> no confirmation needed."""
        monkeypatch.setenv("HFT_ORDER_MODE", "sim")
        monkeypatch.setenv("HFT_MODE", "sim")
        monkeypatch.delenv("HFT_LIVE_CONFIRM", raising=False)
        from hft_platform.services.bootstrap import validate_order_mode_safety

        validate_order_mode_safety()  # should not raise
