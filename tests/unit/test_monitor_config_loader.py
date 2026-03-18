from __future__ import annotations

from pathlib import Path

from hft_platform.monitor._config_loader import load_watchlist


def test_load_watchlist_respects_monitor_env_overrides(
    monkeypatch,
    tmp_path: Path,
) -> None:
    watchlist = tmp_path / "watchlist.yaml"
    symbols = tmp_path / "symbols.yaml"

    watchlist.write_text(
        """
monitor:
  replay_ticks: 64
  batch_limit_per_symbol: 200
symbols:
  - code: TMFC6
    alpha_ids: [queue_imbalance]
""".strip()
    )
    symbols.write_text(
        """
symbols:
  - code: TMFC6
    name: 微台
    product_type: future
""".strip()
    )

    monkeypatch.setenv("HFT_MONITOR_REPLAY_TICKS", "16")
    monkeypatch.setenv("HFT_MONITOR_BATCH_LIMIT_PER_SYMBOL", "48")
    monkeypatch.setenv("HFT_MONITOR_SOURCE", "redis")
    monkeypatch.setenv("HFT_MONITOR_REDIS_HOST", "127.0.0.1")
    monkeypatch.setenv("HFT_MONITOR_REDIS_PORT", "6380")

    config = load_watchlist(watchlist, symbols)

    assert config.replay_ticks == 16
    assert config.batch_limit_per_symbol == 48
    assert config.source == "redis"
    assert config.redis_host == "127.0.0.1"
    assert config.redis_port == 6380
