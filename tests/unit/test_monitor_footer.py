"""Tests for build_footer() context-aware footer help bar."""

from hft_platform.monitor._renderer import build_footer


def test_build_footer_default_context():
    text = build_footer(detail_visible=False, paused=False, has_warnings=False, show_help=False)
    assert "[Space]" in text.plain
    assert "[?]" in text.plain


def test_build_footer_detail_context():
    text = build_footer(detail_visible=True, paused=False, has_warnings=False, show_help=False)
    assert "[ESC]" in text.plain
    assert "[l]" in text.plain


def test_build_footer_paused_context():
    text = build_footer(detail_visible=False, paused=True, has_warnings=False, show_help=False)
    assert "[p]" in text.plain


def test_build_footer_warnings_context():
    text = build_footer(detail_visible=False, paused=False, has_warnings=True, show_help=False)
    assert "[w]" in text.plain
    assert "[R]" in text.plain


def test_build_footer_help_overlay():
    text = build_footer(detail_visible=False, paused=False, has_warnings=False, show_help=True)
    assert "Press any key" in text.plain
