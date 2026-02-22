import os
import sys

sys.path.append(os.getcwd())

from research.alphas.ofi_mc.impl import OFIMCAlpha


def test_update_is_deterministic() -> None:
    ticks = [
        dict(bid_px=100.0, bid_qty=10.0, ask_px=101.0, ask_qty=8.0, trade_vol=1.0, current_mid=100.5),
        dict(bid_px=100.0, bid_qty=11.0, ask_px=101.0, ask_qty=8.0, trade_vol=2.0, current_mid=100.5),
        dict(bid_px=101.0, bid_qty=9.0, ask_px=102.0, ask_qty=7.0, trade_vol=3.0, current_mid=101.5),
    ]
    alpha_a = OFIMCAlpha()
    alpha_b = OFIMCAlpha()
    out_a = [alpha_a.update(**tick) for tick in ticks]
    out_b = [alpha_b.update(**tick) for tick in ticks]
    assert out_a == out_b
