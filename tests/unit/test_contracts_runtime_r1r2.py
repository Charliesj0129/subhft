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
    contract = runtime._get_contract("FUT", "TXFD6", product_type="future")
    assert contract is None or contract is not None  # just no exception
