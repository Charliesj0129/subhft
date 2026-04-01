"""Tests for bot app startup wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def test_main_starts_health_server_before_polling() -> None:
    """Bot main() should start the background health server."""
    import hft_platform.bot.app as mod

    fake_app = MagicMock()

    with (
        patch.object(mod, "_start_health_server_background") as mock_health,
        patch.object(mod, "create_app", return_value=fake_app),
    ):
        mod.main()

    mock_health.assert_called_once_with()
    fake_app.run_polling.assert_called_once_with(drop_pending_updates=True)
