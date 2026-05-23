"""Fix 1 regression: refresh_contracts_and_symbols must not overwrite a
QuoteConnectionPool per-conn shard.

2026-05-23 root cause — ``refresh_contracts_and_symbols`` was calling
``write_symbols_yaml(build_result.symbols, self._client.config_path)`` which,
in pool mode, pointed at ``/tmp/hft_quote_pool_*/symbols_group_<id>.yaml`` —
the partition shard owned by ``QuoteConnectionPool``. The hourly refresh
therefore promoted ``symbols_group_0.yaml`` from ~120 partition entries to
the full ~478 universe, leading to per-conn cap saturation, duplicate
subscriptions across conns, and the misleading hourly "Subscription limit
reached" critical log.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from hft_platform.feed_adapter.shioaji.client import ShioajiClient


def _make_pool_shard(tmp_path):
    pool_dir = tmp_path / "hft_quote_pool_test"
    pool_dir.mkdir()
    shard = pool_dir / "symbols_group_0.yaml"
    shard.write_text(
        "symbols:\n"
        "  - code: '2330'\n"
        "    exchange: TSE\n"
        "    product_type: stock\n"
        "  - code: '2317'\n"
        "    exchange: TSE\n"
        "    product_type: stock\n",
        encoding="utf-8",
    )
    return shard


def _make_canonical(tmp_path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text(
        "symbols:\n  - code: '2330'\n    exchange: TSE\n    product_type: stock\n",
        encoding="utf-8",
    )
    return cfg


def _refresh(client):
    """Invoke refresh_contracts_and_symbols with all broker side-effects mocked.

    Mocks ``_ensure_contracts`` (no broker call), patches ``build_symbols`` to
    return a fabricated wide universe (simulating the 478-vs-shard mismatch),
    and observes whether ``write_symbols_yaml`` is invoked.
    """
    rebuilt = [{"code": f"FAKE{i}", "exchange": "TSE", "product_type": "stock"} for i in range(50)]
    build_result = MagicMock(symbols=rebuilt, errors=[])

    with (
        patch.object(client, "_ensure_contracts"),
        patch("hft_platform.config.symbols.build_symbols", return_value=build_result),
        patch("hft_platform.config.symbols.write_symbols_yaml") as mock_write,
        patch("hft_platform.config.symbols.write_contract_cache"),
    ):
        # Avoid broker API enumeration — point Contracts at empty mocks
        client.api = MagicMock()
        client.api.Contracts.Stocks.TSE = []
        client.api.Contracts.Stocks.OTC = []
        client.api.Contracts.Futures.keys.return_value = []
        client.api.Contracts.Options.keys.return_value = []
        client._contracts_runtime.refresh_contracts_and_symbols()
        return mock_write


def test_pool_shard_not_overwritten_by_contract_refresh(tmp_path):
    """In pool mode, refresh must NOT call write_symbols_yaml on the shard."""
    shard = _make_pool_shard(tmp_path)
    original_bytes = shard.read_bytes()
    original_mtime = shard.stat().st_mtime_ns

    with patch("hft_platform.feed_adapter.shioaji.client.sj"):
        client = ShioajiClient(config_path=str(shard))
        try:
            mock_write = _refresh(client)
            assert not mock_write.called, (
                "write_symbols_yaml must NOT be invoked when config_path lives under "
                f"/hft_quote_pool_/. Called with: {mock_write.call_args_list}"
            )
            # Shard must be byte-identical
            assert shard.read_bytes() == original_bytes
            assert shard.stat().st_mtime_ns == original_mtime
        finally:
            client.close()


def test_canonical_path_still_writes_symbols_yaml(tmp_path):
    """In single-facade mode (canonical config path), refresh continues to
    write back rebuilt symbols — preserving existing behaviour for non-pool
    deployments and tests."""
    cfg = _make_canonical(tmp_path)
    assert "hft_quote_pool_" not in str(cfg)

    with patch("hft_platform.feed_adapter.shioaji.client.sj"):
        client = ShioajiClient(config_path=str(cfg))
        try:
            mock_write = _refresh(client)
            assert mock_write.called, (
                "write_symbols_yaml MUST still run for canonical (non-pool) "
                "config paths; otherwise single-facade deployments lose symbol updates."
            )
            # First positional arg is the rebuilt symbol list, second is the path
            args = mock_write.call_args.args
            assert len(args[0]) == 50  # the fabricated rebuilt universe
            assert os.fspath(args[1]) == str(cfg)
        finally:
            client.close()
