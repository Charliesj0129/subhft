"""Tests for build_help_overlay() full-screen keybinding help."""

from hft_platform.monitor._renderer import build_help_overlay


def test_help_overlay_contains_all_key_categories():
    panel = build_help_overlay()
    text = panel.renderable.plain if hasattr(panel.renderable, "plain") else str(panel.renderable)
    assert "Navigation" in text
    assert "Data" in text
    assert "System" in text
    assert "Space" in text
    assert "Ctrl+R" in text
    assert "ESC" in text
