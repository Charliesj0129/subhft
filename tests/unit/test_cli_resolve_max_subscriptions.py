"""Loop-aware --max-subscriptions resolver (loop_v1 L2)."""

from __future__ import annotations

import argparse

from hft_platform.cli._symbols import _resolve_max_subscriptions


def _ns(*, loop_id: str | None = None, max_subscriptions: int | None = None) -> argparse.Namespace:
    return argparse.Namespace(loop_id=loop_id, max_subscriptions=max_subscriptions)


def test_default_without_loop_is_legacy_480():
    assert _resolve_max_subscriptions(_ns()) == 480


def test_default_with_loop_is_eight():
    assert _resolve_max_subscriptions(_ns(loop_id="r47_tmf_v1")) == 8


def test_explicit_value_wins_without_loop():
    assert _resolve_max_subscriptions(_ns(max_subscriptions=100)) == 100


def test_explicit_value_wins_with_loop():
    """User explicitly sets a cap — the loop-aware default does not override."""
    assert _resolve_max_subscriptions(_ns(loop_id="r47_tmf_v1", max_subscriptions=4)) == 4


def test_explicit_zero_is_respected():
    """An explicit 0 (no subscriptions) is a valid user choice, not a sentinel."""
    assert _resolve_max_subscriptions(_ns(max_subscriptions=0)) == 0


def test_missing_loop_id_attribute_does_not_raise():
    ns = argparse.Namespace(max_subscriptions=None)
    assert _resolve_max_subscriptions(ns) == 480


def test_missing_max_subscriptions_attribute_does_not_raise():
    ns = argparse.Namespace(loop_id="r47_tmf_v1")
    assert _resolve_max_subscriptions(ns) == 8
