from research.calibration.probe_strategy import PassiveQuoteProbe


def test_passive_probe_generates_quotes_on_tick():
    probe = PassiveQuoteProbe(qty=1, max_pos=3)
    action = probe.on_tick(bid=17000, ask=17001, mid=17000.5, position=0)
    assert action.post_bid_price == 17000
    assert action.post_ask_price == 17001
    assert action.qty == 1


def test_passive_probe_respects_max_pos_long():
    probe = PassiveQuoteProbe(qty=1, max_pos=3)
    action = probe.on_tick(bid=17000, ask=17001, mid=17000.5, position=3)
    assert action.post_bid_price is None  # stop bidding
    assert action.post_ask_price == 17001  # still offering


def test_passive_probe_respects_max_pos_short():
    probe = PassiveQuoteProbe(qty=1, max_pos=3)
    action = probe.on_tick(bid=17000, ask=17001, mid=17000.5, position=-3)
    assert action.post_bid_price == 17000  # still bidding
    assert action.post_ask_price is None  # stop offering


def test_passive_probe_zero_spread_stands_back():
    probe = PassiveQuoteProbe(qty=1, max_pos=3)
    action = probe.on_tick(bid=17000, ask=17000, mid=17000.0, position=0)
    assert action.post_bid_price is None
    assert action.post_ask_price is None
