"""Tests for continuous futures contract resolution (R1/R2) in ContractsRuntime."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hft_platform.feed_adapter.shioaji_client import ShioajiClient


def _build_client(tmp_path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text(
        "symbols:\n  - code: 'TXFR1'\n    exchange: 'TAIFEX'\n    product_type: 'futures'\n",
        encoding="utf-8",
    )
    patcher = patch("hft_platform.feed_adapter.shioaji_client.sj")
    mock_sj = patcher.start()
    mock_api = MagicMock()
    mock_sj.Shioaji.return_value = mock_api
    client = ShioajiClient(config_path=str(cfg))
    client.api = mock_api
    return client, patcher


class TestResolveContinuousFuture:
    """Tests for _resolve_continuous_future helper method."""

    def test_txfr1_resolves_via_nested_attr(self, tmp_path: object) -> None:
        client, patcher = _build_client(tmp_path)
        try:
            mock_contract = MagicMock()
            mock_contract.code = "TXFR1"
            txf_group = MagicMock()
            txf_group.TXFR1 = mock_contract
            client.api.Contracts.Futures.TXF = txf_group

            result = client._contracts_runtime._resolve_continuous_future("TXFR1")
            assert result is mock_contract
        finally:
            client.close()
            patcher.stop()

    def test_mxfr2_resolves_via_nested_attr(self, tmp_path: object) -> None:
        client, patcher = _build_client(tmp_path)
        try:
            mock_contract = MagicMock()
            mock_contract.code = "MXFR2"
            mxf_group = MagicMock()
            mxf_group.MXFR2 = mock_contract
            client.api.Contracts.Futures.MXF = mxf_group

            result = client._contracts_runtime._resolve_continuous_future("MXFR2")
            assert result is mock_contract
        finally:
            client.close()
            patcher.stop()

    def test_returns_none_when_root_not_found(self, tmp_path: object) -> None:
        client, patcher = _build_client(tmp_path)
        try:
            client.api.Contracts.Futures.ZZZ = None

            result = client._contracts_runtime._resolve_continuous_future("ZZZR1")
            assert result is None
        finally:
            client.close()
            patcher.stop()

    def test_returns_none_when_contract_attr_missing(self, tmp_path: object) -> None:
        client, patcher = _build_client(tmp_path)
        try:
            txf_group = MagicMock(spec=[])  # empty spec = no attributes
            client.api.Contracts.Futures.TXF = txf_group

            result = client._contracts_runtime._resolve_continuous_future("TXFR1")
            assert result is None
        finally:
            client.close()
            patcher.stop()

    def test_returns_none_for_non_r1r2_code(self, tmp_path: object) -> None:
        client, patcher = _build_client(tmp_path)
        try:
            result = client._contracts_runtime._resolve_continuous_future("TXFD6")
            assert result is None
        finally:
            client.close()
            patcher.stop()

    def test_returns_none_for_short_code(self, tmp_path: object) -> None:
        client, patcher = _build_client(tmp_path)
        try:
            result = client._contracts_runtime._resolve_continuous_future("R1")
            assert result is None
            result = client._contracts_runtime._resolve_continuous_future("XR2")
            assert result is None
        finally:
            client.close()
            patcher.stop()

    def test_returns_none_when_api_is_none(self, tmp_path: object) -> None:
        client, patcher = _build_client(tmp_path)
        try:
            client.api = None
            result = client._contracts_runtime._resolve_continuous_future("TXFR1")
            assert result is None
        finally:
            client.close()
            patcher.stop()


class TestExpandFutureCodes:
    """Tests for _expand_future_codes with R1/R2 handling."""

    def test_r1_code_returns_single_candidate(self, tmp_path: object) -> None:
        client, patcher = _build_client(tmp_path)
        try:
            result = client._contracts_runtime._expand_future_codes("TXFR1")
            assert result == ["TXFR1"]
        finally:
            client.close()
            patcher.stop()

    def test_r2_code_returns_single_candidate(self, tmp_path: object) -> None:
        client, patcher = _build_client(tmp_path)
        try:
            result = client._contracts_runtime._expand_future_codes("MXFR2")
            assert result == ["MXFR2"]
        finally:
            client.close()
            patcher.stop()

    def test_legacy_month_code_still_expands(self, tmp_path: object) -> None:
        """TXFD6 should still expand to [TXFD6, TXF202604] — no regression."""
        client, patcher = _build_client(tmp_path)
        try:
            result = client._contracts_runtime._expand_future_codes("TXFD6")
            assert len(result) == 2
            assert result[0] == "TXFD6"
            assert "TXF" in result[1]
            # The second candidate should be TXF + YYYY + 04
            assert result[1].endswith("04")
        finally:
            client.close()
            patcher.stop()

    def test_yyyymm_code_returns_single_candidate(self, tmp_path: object) -> None:
        client, patcher = _build_client(tmp_path)
        try:
            result = client._contracts_runtime._expand_future_codes("TXF202604")
            assert result == ["TXF202604"]
        finally:
            client.close()
            patcher.stop()

    def test_short_code_not_treated_as_continuous(self, tmp_path: object) -> None:
        """Codes shorter than 4 chars ending in R1/R2 should not match."""
        client, patcher = _build_client(tmp_path)
        try:
            # "R1" is len 2, "XR1" is len 3 — neither should match continuous pattern
            result = client._contracts_runtime._expand_future_codes("R1")
            assert result == ["R1"]
            result = client._contracts_runtime._expand_future_codes("XR1")
            assert result == ["XR1"]
        finally:
            client.close()
            patcher.stop()


class TestGetContractContinuousFutures:
    """Integration tests for _get_contract with continuous futures."""

    def test_get_contract_resolves_txfr1(self, tmp_path: object) -> None:
        client, patcher = _build_client(tmp_path)
        try:
            mock_contract = MagicMock()
            mock_contract.code = "TXFR1"

            # Make the normal _lookup_contract fail (returns None)
            # but _resolve_continuous_future succeed
            txf_group = MagicMock()
            txf_group.TXFR1 = mock_contract
            client.api.Contracts.Futures.TXF = txf_group

            # The Futures container itself won't find TXFR1 directly
            client.api.Contracts.Futures.__getitem__ = MagicMock(side_effect=KeyError("TXFR1"))

            result = client._contracts_runtime._get_contract("TAIFEX", "TXFR1", product_type="futures")
            assert result is not None
        finally:
            client.close()
            patcher.stop()

    def test_get_contract_regular_future_no_regression(self, tmp_path: object) -> None:
        """Regular future code should still resolve via _lookup_contract."""
        client, patcher = _build_client(tmp_path)
        try:
            mock_contract = MagicMock()
            mock_contract.code = "TXF202604"

            # Make Futures["TXF202604"] work
            client.api.Contracts.Futures.__getitem__ = MagicMock(return_value=mock_contract)

            result = client._contracts_runtime._get_contract("TAIFEX", "TXF202604", product_type="futures")
            assert result is mock_contract
        finally:
            client.close()
            patcher.stop()
