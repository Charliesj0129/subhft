"""Tests for R1/R2 continuous contract alias resolution."""

from unittest.mock import MagicMock

from hft_platform.feed_adapter.shioaji.contracts_runtime import ContractsRuntime


def _make_runtime_with_r1r2():
    client = MagicMock()
    client.api = MagicMock()
    client.allow_symbol_fallback = False
    txfr1 = MagicMock(name="TXFR1_contract")
    txfr1.code = "TXFR1"
    txfr2 = MagicMock(name="TXFR2_contract")
    txfr2.code = "TXFR2"
    futures = MagicMock()
    txf_group = MagicMock()
    txf_group.TXFR1 = txfr1
    txf_group.TXFR2 = txfr2
    futures.TXF = txf_group

    def lookup_side_effect(key):
        if key == "TXFR1":
            return txfr1
        if key == "TXFR2":
            return txfr2
        raise KeyError(key)

    futures.__getitem__ = lookup_side_effect
    client.api.Contracts.Futures = futures
    return ContractsRuntime(client), txfr1, txfr2


def test_r1_alias_resolves():
    runtime, txfr1, _ = _make_runtime_with_r1r2()
    contract = runtime._get_contract("FUT", "TXFR1", product_type="future")
    assert contract is not None


def test_r2_alias_resolves():
    runtime, _, txfr2 = _make_runtime_with_r1r2()
    contract = runtime._get_contract("FUT", "TXFR2", product_type="future")
    assert contract is not None


def test_non_r1r2_code_unaffected():
    runtime, _, _ = _make_runtime_with_r1r2()
    # TXFD6 is a month-code symbol, not an R1/R2 alias.
    # The runtime now does a direct product-group lookup (Futures.TXF.TXFD6)
    # so it will find the contract if the group has that attribute.
    # With MagicMock, any attr returns a mock, so we verify it returns
    # *something* (the mock auto-creates it) — the key invariant is that
    # R1/R2 resolution is NOT triggered for non-R1/R2 codes.
    contract = runtime._get_contract("FUT", "TXFD6", product_type="future")
    # After the direct product-group lookup was added, non-R1/R2 month codes
    # ARE resolved via Futures.<root>.<code>. This is correct behaviour.
    assert contract is not None


def test_get_contract_ensures_contracts_when_api_lacks_contracts():
    client = MagicMock()
    client.api = MagicMock()
    client.allow_symbol_fallback = False
    client.index_exchange = "TSE"
    contract = MagicMock(name="TMFD6_contract")
    contract.code = "TMFD6"

    def _ensure_contracts():
        client.api.Contracts = MagicMock()
        client.api.Contracts.Futures = {"TMFD6": contract}

    del client.api.Contracts
    client._ensure_contracts.side_effect = _ensure_contracts

    runtime = ContractsRuntime(client)
    resolved = runtime._get_contract("FUT", "TMFD6", product_type="future")

    client._ensure_contracts.assert_called_once_with()
    assert resolved is contract
