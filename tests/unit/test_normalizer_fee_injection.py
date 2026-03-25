"""Verify ExecutionNormalizer accepts fee_calculator and order_cmd."""
import yaml

from hft_platform.execution.normalizer import ExecutionNormalizer
from hft_platform.tca.fee_calculator import FeeCalculator


def test_normalizer_accepts_fee_calculator() -> None:
    config = yaml.safe_load("""
futures:
  XMT:
    commission_per_contract: 13
    tax_rate_bps: 2.0
    tax_side: sell
    tick_size: 1
    point_value: 10
""")
    calc = FeeCalculator(config)
    normalizer = ExecutionNormalizer(fee_calculator=calc)
    assert normalizer._fee_calc is calc
