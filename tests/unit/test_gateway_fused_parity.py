"""Parity tests for RustGatewayFusedCheck — fused dedup+policy+exposure+risk gate."""

import pytest

rust_core = pytest.importorskip("hft_platform.rust_core")

# Skip entire module if the class is not yet registered in lib.rs
if not hasattr(rust_core, "RustGatewayFusedCheck"):
    pytest.skip(
        "RustGatewayFusedCheck not registered in lib.rs yet",
        allow_module_level=True,
    )

RustGatewayFusedCheck = rust_core.RustGatewayFusedCheck

# -- Constants ----------------------------------------------------------------

PRICE_100 = 100 * 10_000  # scaled x10000
QTY_10 = 10
NOW_NS = 1_000_000_000_000  # 1000s in ns
TTL_NS = 5_000_000_000  # 5s


def _make_gate(**overrides) -> RustGatewayFusedCheck:
    """Create a pre-configured gate with sensible defaults."""
    g = RustGatewayFusedCheck()
    g.configure_risk(
        max_notional=overrides.get("max_notional", 0),
        price_band_bps=overrides.get("price_band_bps", 0),
        max_order_qty=overrides.get("max_order_qty", 0),
    )
    g.configure_exposure(
        global_limit=overrides.get("global_limit", 0),
        per_symbol_limit=overrides.get("per_symbol_limit", 0),
    )
    g.configure_dedup(ttl_ns=overrides.get("ttl_ns", TTL_NS))
    return g


# -- 1. Basic approval --------------------------------------------------------


def test_basic_approval():
    g = _make_gate()
    ok, code = g.check_intent("key1", 0, "2330", PRICE_100, QTY_10, "s1", 0, NOW_NS)
    assert ok is True
    assert code == 0


# -- 2. Duplicate rejection ---------------------------------------------------


def test_duplicate_rejection():
    g = _make_gate()
    g.check_intent("key1", 0, "2330", PRICE_100, QTY_10, "s1", 0, NOW_NS)
    ok, code = g.check_intent("key1", 0, "2330", PRICE_100, QTY_10, "s1", 0, NOW_NS + 1)
    assert ok is False
    assert code == 1


# -- 3. Duplicate allowed after TTL -------------------------------------------


def test_duplicate_allowed_after_ttl():
    g = _make_gate(ttl_ns=TTL_NS)
    g.check_intent("key1", 0, "2330", PRICE_100, QTY_10, "s1", 0, NOW_NS)
    ok, code = g.check_intent("key1", 0, "2330", PRICE_100, QTY_10, "s1", 0, NOW_NS + TTL_NS + 1)
    assert ok is True
    assert code == 0


# -- 4. Qty limit rejection ---------------------------------------------------


def test_qty_limit_rejection():
    g = _make_gate(max_order_qty=5)
    ok, code = g.check_intent("key1", 0, "2330", PRICE_100, 6, "s1", 0, NOW_NS)
    assert ok is False
    assert code == 5


# -- 5. Price band rejection ---------------------------------------------------


def test_price_band_rejection():
    ref_price = 100 * 10_000
    # price_band_bps=100 means 1% => max deviation = ref_price * 100 / 10000 = ref_price * 0.01
    # price at 102 * 10000 => deviation = 2*10000, ratio = 2*10000*10000 / (100*10000) = 200 > 100
    g = _make_gate(price_band_bps=100)
    far_price = 102 * 10_000
    ok, code = g.check_intent("key1", 0, "2330", far_price, QTY_10, "s1", ref_price, NOW_NS)
    assert ok is False
    assert code == 4


# -- 6. Per-symbol exposure limit ----------------------------------------------


def test_per_symbol_exposure_limit():
    notional = PRICE_100 * QTY_10  # 1_000_000 * 10 = 10_000_000
    # Set limit just below double the notional
    g = _make_gate(per_symbol_limit=notional * 2 - 1)
    g.check_intent("key1", 0, "2330", PRICE_100, QTY_10, "s1", 0, NOW_NS)
    ok, code = g.check_intent("key2", 0, "2330", PRICE_100, QTY_10, "s1", 0, NOW_NS + 1)
    assert ok is False
    assert code == 3


# -- 7. Global exposure limit -------------------------------------------------


def test_global_exposure_limit():
    notional = PRICE_100 * QTY_10
    g = _make_gate(global_limit=notional * 2 - 1)
    # First on symbol A
    g.check_intent("key1", 0, "2330", PRICE_100, QTY_10, "s1", 0, NOW_NS)
    # Second on symbol B — different symbol but same global pool
    ok, code = g.check_intent("key2", 0, "2317", PRICE_100, QTY_10, "s1", 0, NOW_NS + 1)
    assert ok is False
    assert code == 3


# -- 8. Risk notional limit ---------------------------------------------------


def test_risk_notional_limit():
    notional = PRICE_100 * QTY_10
    g = _make_gate(max_notional=notional - 1)
    ok, code = g.check_intent("key1", 0, "2330", PRICE_100, QTY_10, "s1", 0, NOW_NS)
    assert ok is False
    assert code == 2


# -- 9. reason_str returns correct strings ------------------------------------


def test_reason_str_all_codes():
    assert RustGatewayFusedCheck.reason_str(0) == "approved"
    assert RustGatewayFusedCheck.reason_str(1) == "duplicate"
    assert RustGatewayFusedCheck.reason_str(2) == "risk_limit"
    assert RustGatewayFusedCheck.reason_str(3) == "exposure_limit"
    assert RustGatewayFusedCheck.reason_str(4) == "price_band"
    assert RustGatewayFusedCheck.reason_str(5) == "qty_limit"
    assert RustGatewayFusedCheck.reason_str(255) == "unknown"


# -- 10. Multiple strategies don't interfere ----------------------------------


def test_multiple_strategies_no_interference():
    g = _make_gate()
    # Same symbol, different strategies — dedup is key-based so different keys pass
    ok1, _ = g.check_intent("s1-key1", 0, "2330", PRICE_100, QTY_10, "s1", 0, NOW_NS)
    ok2, _ = g.check_intent("s2-key1", 0, "2330", PRICE_100, QTY_10, "s2", 0, NOW_NS + 1)
    assert ok1 is True
    assert ok2 is True
    # Exposure is symbol-based: both contribute to 2330 exposure
    expected_notional = PRICE_100 * QTY_10 * 2
    assert g.symbol_exposure("2330") == expected_notional


# -- 11. Reset clears all state ------------------------------------------------


def test_reset_clears_all_state():
    g = _make_gate()
    g.check_intent("key1", 0, "2330", PRICE_100, QTY_10, "s1", 0, NOW_NS)
    assert g.global_exposure() > 0
    assert g.symbol_exposure("2330") > 0

    g.reset()

    assert g.global_exposure() == 0
    assert g.symbol_exposure("2330") == 0
    # Key should be reusable after reset
    ok, code = g.check_intent("key1", 0, "2330", PRICE_100, QTY_10, "s1", 0, NOW_NS + 1)
    assert ok is True
    assert code == 0


# -- 12. Configure methods update limits correctly ----------------------------


def test_configure_updates_limits():
    g = RustGatewayFusedCheck()
    # Default: no limits -> everything passes
    ok, _ = g.check_intent("key1", 0, "2330", PRICE_100, 1000, "s1", 0, NOW_NS)
    assert ok is True

    g.reset()
    # Now set qty limit
    g.configure_risk(max_notional=0, price_band_bps=0, max_order_qty=5)
    ok, code = g.check_intent("key2", 0, "2330", PRICE_100, 1000, "s1", 0, NOW_NS + 1)
    assert ok is False
    assert code == 5


# -- 13. Exposure tracking accumulates correctly -------------------------------


def test_exposure_accumulates():
    g = _make_gate()
    notional = PRICE_100 * QTY_10

    g.check_intent("key1", 0, "2330", PRICE_100, QTY_10, "s1", 0, NOW_NS)
    assert g.global_exposure() == notional
    assert g.symbol_exposure("2330") == notional

    g.check_intent("key2", 0, "2330", PRICE_100, QTY_10, "s1", 0, NOW_NS + 1)
    assert g.global_exposure() == notional * 2
    assert g.symbol_exposure("2330") == notional * 2

    g.check_intent("key3", 0, "2317", PRICE_100, QTY_10, "s1", 0, NOW_NS + 2)
    assert g.global_exposure() == notional * 3
    assert g.symbol_exposure("2317") == notional
    assert g.symbol_exposure("2330") == notional * 2
