from __future__ import annotations

from pathlib import Path

from hft_platform.monitor._alpha_dispatcher import _call_alpha, _probe_dispatch_keys
from hft_platform.monitor._config_loader import _is_expired_contract, load_watchlist
from hft_platform.monitor._types import AlphaState
from research.registry.alpha_registry import AlphaRegistry


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


def test_auto_derive_from_symbol_source(tmp_path: Path) -> None:
    """S5: symbol_source derives watchlist from referenced symbols file."""
    source_file = tmp_path / "symbols_src.yaml"
    source_file.write_text(
        """
symbols:
  - code: "2330"
    name: 台積電
    product_type: stock
  - code: "2317"
    name: 鴻海
    product_type: stock
""".strip()
    )

    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text(
        f"""
monitor:
  symbol_source: "{source_file}"
  default_alpha_ids: [queue_imbalance, microprice_momentum]
symbols: []
""".strip()
    )

    symbols = tmp_path / "symbols.yaml"
    symbols.write_text("symbols: []")

    config = load_watchlist(watchlist, symbols)

    assert len(config.symbols) == 2
    codes = [s.code for s in config.symbols]
    assert "2330" in codes
    assert "2317" in codes
    assert config.symbols[0].alpha_ids == ("queue_imbalance", "microprice_momentum")


def test_auto_derive_filters_expired_contracts(tmp_path: Path) -> None:
    """S5: expired contracts are filtered when auto_filter_skip_expired is True."""
    source_file = tmp_path / "symbols_src.yaml"
    source_file.write_text(
        """
symbols:
  - code: TMFC6
    name: 微台03
    product_type: future
  - code: TMFE6
    name: 微台05
    product_type: future
""".strip()
    )

    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text(
        f"""
monitor:
  symbol_source: "{source_file}"
  auto_filter_skip_expired: true
  default_alpha_ids: [queue_imbalance]
symbols: []
""".strip()
    )

    symbols = tmp_path / "symbols.yaml"
    symbols.write_text("symbols: []")

    config = load_watchlist(watchlist, symbols)

    codes = [s.code for s in config.symbols]
    # TMFC6 (March) should be filtered because TMFE6 (May) exists
    assert "TMFC6" not in codes
    assert "TMFE6" in codes


def test_auto_derive_pin_symbols_bypass_filter(tmp_path: Path) -> None:
    """S5: pin_symbols are never filtered even if expired."""
    source_file = tmp_path / "symbols_src.yaml"
    source_file.write_text(
        """
symbols:
  - code: TMFC6
    name: 微台03
    product_type: future
  - code: TMFE6
    name: 微台05
    product_type: future
""".strip()
    )

    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text(
        f"""
monitor:
  symbol_source: "{source_file}"
  auto_filter_skip_expired: true
  pin_symbols: ["TMFC6"]
  default_alpha_ids: [queue_imbalance]
symbols: []
""".strip()
    )

    symbols = tmp_path / "symbols.yaml"
    symbols.write_text("symbols: []")

    config = load_watchlist(watchlist, symbols)

    codes = [s.code for s in config.symbols]
    # TMFC6 should be kept because it's pinned
    assert "TMFC6" in codes
    assert "TMFE6" in codes


def test_backward_compat_no_symbol_source(tmp_path: Path) -> None:
    """S5: existing watchlist without symbol_source works unchanged."""
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text(
        """
monitor: {}
symbols:
  - code: TMFC6
    alpha_ids: [queue_imbalance]
""".strip()
    )

    symbols = tmp_path / "symbols.yaml"
    symbols.write_text(
        """
symbols:
  - code: TMFC6
    name: 微台
    product_type: future
""".strip()
    )

    config = load_watchlist(watchlist, symbols)
    assert len(config.symbols) == 1
    assert config.symbols[0].code == "TMFC6"
    assert config.symbol_source == ""


def test_explicit_symbols_inherit_default_alpha_ids(tmp_path: Path) -> None:
    """Explicit watchlist symbols should inherit monitor.default_alpha_ids."""
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text(
        """
monitor:
  default_alpha_ids: [ofi_surprise]
symbols:
  - code: TMFC6
    alpha_ids: []
""".strip()
    )

    symbols = tmp_path / "symbols.yaml"
    symbols.write_text(
        """
symbols:
  - code: TMFC6
    name: 微台
    product_type: future
""".strip()
    )

    config = load_watchlist(watchlist, symbols)

    assert len(config.symbols) == 1
    assert config.symbols[0].alpha_ids == ("ofi_surprise",)


def test_is_expired_contract() -> None:
    """S5: expired contract detection for futures month codes."""
    all_codes = {"TMFC6", "TMFE6", "TMFG6"}
    # C (March) is expired because E (May) exists
    assert _is_expired_contract("TMFC6", all_codes) is True
    # E (May) is expired because G (July) exists
    assert _is_expired_contract("TMFE6", all_codes) is True
    # G (July) is the latest — not expired
    assert _is_expired_contract("TMFG6", all_codes) is False
    # Stock codes are never expired
    assert _is_expired_contract("2330", {"2330"}) is False


def test_repo_watchlist_default_alphas_are_discoverable_and_monitor_compatible() -> None:
    """Shipped watchlist must only reference alphas the monitor can actually run."""
    config = load_watchlist(Path("config/watchlist.yaml"), Path("config/symbols.yaml"))

    registry = AlphaRegistry()
    discovered = registry.discover("research/alphas")

    sample_payload = {
        "bid_px": 100.0,
        "ask_px": 100.1,
        "bid_qty": 10.0,
        "ask_qty": 12.0,
        "mid_price": 100.05,
        "microprice_x2": 2001000,
        "spread_scaled": 1000,
        "spread_bps": 9.995,
        "imbalance": -0.0909,
        "ofi_l1_raw": -2.0,
        "ofi_l1_cum": -2.0,
        "local_ts": 1,
    }

    missing: list[str] = []
    incompatible: list[str] = []

    for alpha_id in config.default_alpha_ids:
        alpha = discovered.get(alpha_id)
        if alpha is None:
            missing.append(alpha_id)
            continue

        dispatch_keys = _probe_dispatch_keys(alpha)
        filtered_buf = {k: 0.0 for k in dispatch_keys} if dispatch_keys else {}
        state = AlphaState(
            alpha_id=alpha_id,
            runtime=alpha,
            _dispatch_keys=dispatch_keys,
            _filtered_buf=filtered_buf,
        )
        signal = _call_alpha(state, sample_payload)
        if signal is None:
            incompatible.append(alpha_id)

    assert not missing, f"watchlist references undiscoverable alphas: {missing}"
    assert not incompatible, f"watchlist references monitor-incompatible alphas: {incompatible}"
