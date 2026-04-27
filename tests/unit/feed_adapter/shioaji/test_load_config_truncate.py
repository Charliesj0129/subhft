"""Q3 / RC-1 (2026-04-27): cover ``ShioajiClient._load_config`` exit branches.

Bug context
-----------
Pre-fix, ``_load_config`` raised ``ValueError`` whenever
``len(symbols) > MAX_SUBSCRIPTIONS`` and the
``HFT_ALLOW_TRUNCATE_SUBSCRIPTIONS`` env var was unset. Production logs
showed this firing every ~25min on the 588-contract universe vs. the
120-cap ``MAX_SUBSCRIPTIONS`` constant — silently breaking the reload
cycle because no Counter / alert / Telegram path was wired.

Post-fix the comparison is against the new
``MAX_SUBSCRIPTIONS_PER_CLIENT`` (default 600, env-overridable via
``HFT_MAX_SUBSCRIPTIONS``) and truncate-with-warn is the default. The
``feed_symbol_config_reload_total{result}`` Counter is bumped at every
exit branch.

These tests cover only the ``_load_config`` path — they do not exercise
broker SDK calls, so they run quickly without network.
"""

from __future__ import annotations

import yaml

from hft_platform.feed_adapter.shioaji_client import ShioajiClient
from hft_platform.observability.metrics import MetricsRegistry


def _counter_value(metric, **labels) -> float:
    """Read the current value of a Prometheus ``Counter`` for ``labels``."""
    return metric.labels(**labels)._value.get()


def _make_symbols(count: int) -> list[dict]:
    return [{"code": f"S{i:04d}", "exchange": "TWSE"} for i in range(count)]


def test_load_config_truncates_when_over_per_client_limit(tmp_path, monkeypatch):
    """588 > 120 (per-conn) but < 600 (per-client default) → ok branch.

    P2 #8 (2026-04-27): the ``ok`` branch now ALSO requires that the
    universe fits within ``HFT_QUOTE_CONNECTIONS × per-conn cap``,
    otherwise an ``exceeds_pool_capacity`` advisory fires. To preserve
    this test's original intent (proving 588 ≤ 600 per-client doesn't
    truncate), we set ``HFT_QUOTE_CONNECTIONS=5`` so 5×120=600 ≥ 588.
    """
    config_path = tmp_path / "symbols.yaml"
    config_path.write_text(yaml.dump({"symbols": _make_symbols(588)}))

    metrics = MetricsRegistry.get()
    counter = metrics.feed_symbol_config_reload_total
    before_ok = _counter_value(counter, result="ok")

    monkeypatch.setenv("HFT_QUOTE_CONNECTIONS", "5")
    monkeypatch.delenv("HFT_ALLOW_TRUNCATE_SUBSCRIPTIONS", raising=False)
    monkeypatch.delenv("HFT_STRICT_SUBSCRIPTION_LIMIT", raising=False)
    monkeypatch.delenv("HFT_MAX_SUBSCRIPTIONS", raising=False)

    client = ShioajiClient(config_path=str(config_path))

    # 588 ≤ 600 (default per-client ceiling) → "ok" branch, no truncation.
    assert len(client.symbols) == 588
    after_ok = _counter_value(counter, result="ok")
    assert after_ok - before_ok >= 1.0, "ok branch must bump the metric"


def test_load_config_truncate_with_warn_when_exceeding_per_client_ceiling(tmp_path, monkeypatch):
    """700 > 600 (per-client default) → truncate-with-warn (new default)."""
    config_path = tmp_path / "symbols.yaml"
    config_path.write_text(yaml.dump({"symbols": _make_symbols(700)}))

    metrics = MetricsRegistry.get()
    counter = metrics.feed_symbol_config_reload_total
    before_exceeds = _counter_value(counter, result="exceeds_limit")

    # New default: truncate-with-warn (HFT_ALLOW_TRUNCATE_SUBSCRIPTIONS unset
    # ⇒ truncate). HFT_STRICT_SUBSCRIPTION_LIMIT also unset ⇒ no raise.
    monkeypatch.delenv("HFT_ALLOW_TRUNCATE_SUBSCRIPTIONS", raising=False)
    monkeypatch.delenv("HFT_STRICT_SUBSCRIPTION_LIMIT", raising=False)
    monkeypatch.delenv("HFT_MAX_SUBSCRIPTIONS", raising=False)

    client = ShioajiClient(config_path=str(config_path))

    # Truncated to per-client ceiling (default 600).
    assert len(client.symbols) == client.MAX_SUBSCRIPTIONS_PER_CLIENT == 600
    after_exceeds = _counter_value(counter, result="exceeds_limit")
    assert after_exceeds - before_exceeds == 1.0, (
        "exceeds_limit branch must bump the metric exactly once"
    )


def test_load_config_strict_mode_still_raises(tmp_path, monkeypatch):
    """``HFT_STRICT_SUBSCRIPTION_LIMIT=1`` keeps legacy raise behaviour."""
    import pytest as _pytest

    config_path = tmp_path / "symbols.yaml"
    config_path.write_text(yaml.dump({"symbols": _make_symbols(700)}))

    metrics = MetricsRegistry.get()
    counter = metrics.feed_symbol_config_reload_total
    before_exceeds = _counter_value(counter, result="exceeds_limit")

    monkeypatch.setenv("HFT_STRICT_SUBSCRIPTION_LIMIT", "1")
    monkeypatch.delenv("HFT_MAX_SUBSCRIPTIONS", raising=False)

    with _pytest.raises(ValueError, match="exceeds limit"):
        ShioajiClient(config_path=str(config_path))

    after_exceeds = _counter_value(counter, result="exceeds_limit")
    assert after_exceeds - before_exceeds == 1.0, (
        "strict-mode raise must still bump the exceeds_limit Counter"
    )


def test_load_config_per_client_ceiling_env_override(tmp_path, monkeypatch):
    """``HFT_MAX_SUBSCRIPTIONS=800`` lets a 700-symbol universe load cleanly."""
    config_path = tmp_path / "symbols.yaml"
    config_path.write_text(yaml.dump({"symbols": _make_symbols(700)}))

    # Env override is consumed at limits-module import time. Reload the
    # module so the new value takes effect for this test only.
    monkeypatch.setenv("HFT_MAX_SUBSCRIPTIONS", "800")
    import importlib

    from hft_platform.feed_adapter.shioaji import limits as _limits_mod

    importlib.reload(_limits_mod)
    assert _limits_mod.DEFAULT_MAX_SUBSCRIPTIONS_PER_CLIENT == 800

    # Re-import client module so it re-binds to the reloaded default. The
    # client constructor reads the constant via the imported name; reloading
    # the limits module is sufficient because the client reads the per-client
    # default lazily at __init__ via the module-level constant.
    from hft_platform.feed_adapter.shioaji import client as _client_mod

    importlib.reload(_client_mod)
    new_client = _client_mod.ShioajiClient(config_path=str(config_path))
    assert new_client.MAX_SUBSCRIPTIONS_PER_CLIENT == 800
    assert len(new_client.symbols) == 700
