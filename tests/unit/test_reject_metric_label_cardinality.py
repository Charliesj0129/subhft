"""Reject-reason metric labels must be bounded.

``risk_reject_total`` and ``gateway_reject_total`` both label by the rejection
reason, and both are fed ``RiskDecision.reason_code`` -- a field whose name says
"code" but whose value is a formatted message carrying live PnL, notional,
price and quantity figures. Every distinct figure minted a Prometheus series
that is never freed for the life of the process, and a matching permanent entry
in each caller's label cache.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hft_platform.observability.metrics import cap_reject_reason

# The eight reason templates the risk path can produce, with the runtime values
# that made each one unbounded.
_SOFT_LIMIT_VARIANTS = [
    "SOFT_LIMIT: loss=-11 >= threshold=99",
    "SOFT_LIMIT: loss=101 >= threshold=99",
    "SOFT_LIMIT: loss=250 >= threshold=99",
]


class _FakeChild:
    def __init__(self) -> None:
        self.count = 0

    def inc(self) -> None:
        self.count += 1


class _FakeCounter:
    """Stands in for a Prometheus Counter, recording every distinct label set."""

    def __init__(self) -> None:
        self.children: dict[tuple, _FakeChild] = {}

    def labels(self, **kwargs) -> _FakeChild:
        key = tuple(sorted(kwargs.items()))
        child = self.children.get(key)
        if child is None:
            child = _FakeChild()
            self.children[key] = child
        return child


def test_cap_reject_reason_collapses_formatted_pnl_detail():
    """Three rejections differing only by a loss figure are one series."""
    codes = {cap_reject_reason(r) for r in _SOFT_LIMIT_VARIANTS}
    assert codes == {"SOFT_LIMIT"}


def test_cap_reject_reason_strips_a_parenthesised_symbol():
    """``PRICE_EXCEEDS_CAP`` embeds the symbol *and* a near-continuous price."""
    a = cap_reject_reason("PRICE_EXCEEDS_CAP(TXFI6): 1234500 > 1200000")
    b = cap_reject_reason("PRICE_EXCEEDS_CAP(TMFI6): 999900 > 900000")
    assert a == b == "PRICE_EXCEEDS_CAP"


def test_cap_reject_reason_passes_a_bare_code_through():
    """A reason that is already a code must survive unchanged."""
    assert cap_reject_reason("STORMGUARD_STORM_BLOCKED") == "STORMGUARD_STORM_BLOCKED"
    assert cap_reject_reason("live_order_ttl_expired") == "live_order_ttl_expired"


def test_cap_reject_reason_buckets_anything_not_shaped_like_a_code():
    """Free-form text is what an unbounded label looks like; bucket it."""
    assert cap_reject_reason("broker said no, order 8891 at 09:14") == "OTHER"
    assert cap_reject_reason("") == "OTHER"
    assert cap_reject_reason("   ") == "OTHER"
    assert cap_reject_reason("9LIVES") == "OTHER"
    assert cap_reject_reason("X" * 65) == "OTHER"


def test_risk_reject_label_is_one_series_across_distinct_pnl_values():
    """Regression: the risk counter minted a series per loss figure."""
    from hft_platform.risk.engine import RiskEngine

    counter = _FakeCounter()
    engine = SimpleNamespace(
        metrics=SimpleNamespace(risk_reject_total=counter),
        _reject_metric_counter=0,
        _reject_metric_sample_every=1,
        _reject_metric_cache={},
        _reject_metric_cache_owner_id=None,
    )
    for reason in _SOFT_LIMIT_VARIANTS:
        RiskEngine._emit_reject_metric(engine, "R47_MAKER_TMF", reason)

    assert len(counter.children) == 1, f"expected 1 series, got {list(counter.children)}"
    assert len(engine._reject_metric_cache) == 1
    assert sum(c.count for c in counter.children.values()) == 3


def test_gateway_reject_label_is_one_series_across_distinct_pnl_values():
    """The gateway counter is fed the same strings and had the same defect."""
    from hft_platform.gateway.service import GatewayService

    counter = _FakeCounter()
    svc = SimpleNamespace(
        _metrics_enabled=True,
        _gateway_reject_counter=0,
        _gateway_reject_sample_every=1,
        _gateway_reject_metric_cache={},
        _metrics_or_refresh=lambda: SimpleNamespace(gateway_reject_total=counter),
    )
    for reason in _SOFT_LIMIT_VARIANTS:
        GatewayService._emit_reject(svc, reason)

    assert len(counter.children) == 1, f"expected 1 series, got {list(counter.children)}"
    assert len(svc._gateway_reject_metric_cache) == 1
    assert sum(c.count for c in counter.children.values()) == 3


def test_distinct_gates_still_get_distinct_series():
    """Bounding the label must not merge unrelated gates into one line."""
    reasons = [
        "SOFT_LIMIT: loss=101 >= threshold=99",
        "STORMGUARD_STORM_BLOCKED",
        "POSITION_LIMIT_EXCEEDED: abs(3) > 1",
        "DAILY_LOSS_LIMIT_EXCEEDED: loss=101 >= limit=99",
        "PEAK_DRAWDOWN: drawdown=120 > limit=100",
        "MAX_NOTIONAL_EXCEEDED: 500 > 400",
        "PER_SYMBOL_NOTIONAL_EXCEEDED: 500 > 400",
        "PRICE_EXCEEDS_CAP(TXFI6): 1234500 > 1200000",
    ]
    assert len({cap_reject_reason(r) for r in reasons}) == len(reasons)


def test_the_full_reason_is_still_logged_verbatim():
    """Nothing is lost: the detail moves to the log, not out of the system."""
    import inspect

    from hft_platform.risk.engine import RiskEngine

    src = inspect.getsource(RiskEngine)
    assert 'logger.warning("Order Rejected by Risk", sid=intent.strategy_id, reason=decision.reason_code)' in src, (
        "the rejection log line must keep carrying the unabridged reason"
    )
