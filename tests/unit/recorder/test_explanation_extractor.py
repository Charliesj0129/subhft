"""Tests for the L8 ``explanations`` topic extractor in ``recorder/worker.py``.

Validates that ``_extract_explanation_values`` produces a list aligned with
``EXPLANATION_COLUMNS`` for both ``OrderExplanation`` instances and dict
inputs, and that JSON-shaped fields round-trip via the ZSTD-encoded
``String`` columns in ``hft.order_explanations``.
"""

from __future__ import annotations

import json

from hft_platform.order.explanation import OrderExplanation
from hft_platform.recorder.worker import (
    _EXTRACTOR_COLUMNS,
    EXPLANATION_COLUMNS,
    _extract_explanation_values,
)


def _make_explanation() -> OrderExplanation:
    return OrderExplanation(
        trace_id="trace-abc",
        client_order_id="R47:42",
        loop_id="r47_tmf_v1",
        strategy_id="R47_MAKER_TMF",
        strategy_version="1823be17",
        config_hash="deadbeefcafe0001",
        git_sha="1823be17",
        data_session_id="sim-2026-05-05",
        symbol="TMFR1",
        feature_snapshot={"spread_pts": 5, "qi": 0.14},
        strategy_decision={"reason": "spread>=threshold"},
        risk_decision={"approved": True, "reason_code": "OK"},
        order={"price_scaled": 171960000, "qty": 1, "side": "BUY"},
        fills=[{"fill_id": "f-0", "qty": 1, "price_scaled": 171960000}],
        cancels=[],
        pnl_after=None,
        lifecycle_status="filled",
        ts_emit=1_700_000_000_000_000_000,
    )


class TestExplanationExtractor:
    def test_columns_registered_in_extractor_columns_map(self) -> None:
        assert _EXTRACTOR_COLUMNS["explanations"] is EXPLANATION_COLUMNS
        # The extractor expects exactly 18 columns; lock the contract so a
        # future column addition can't silently desync the value list and the
        # CK INSERT column list.
        assert len(EXPLANATION_COLUMNS) == 18

    def test_extract_from_dataclass(self) -> None:
        e = _make_explanation()
        values = _extract_explanation_values(e)
        assert values is not None
        assert len(values) == len(EXPLANATION_COLUMNS)

        # Spot-check key positions.
        col_idx = {name: i for i, name in enumerate(EXPLANATION_COLUMNS)}
        assert values[col_idx["trace_id"]] == "trace-abc"
        assert values[col_idx["client_order_id"]] == "R47:42"
        assert values[col_idx["loop_id"]] == "r47_tmf_v1"
        assert values[col_idx["lifecycle_status"]] == "filled"
        assert values[col_idx["ts_emit"]] == 1_700_000_000_000_000_000

        # JSON fields should be valid JSON strings round-trippable to the
        # original dict/list payload.
        decoded_feat = json.loads(values[col_idx["feature_snapshot"]])
        assert decoded_feat == {"spread_pts": 5, "qi": 0.14}
        decoded_fills = json.loads(values[col_idx["fills"]])
        assert decoded_fills == [{"fill_id": "f-0", "qty": 1, "price_scaled": 171960000}]

    def test_extract_from_dict(self) -> None:
        payload = {
            "trace_id": "trace-dict",
            "client_order_id": "R47:99",
            "loop_id": "r47_tmf_v1",
            "strategy_id": "R47_MAKER_TMF",
            "strategy_version": "1823be17",
            "config_hash": "deadbeefcafe0001",
            "git_sha": "1823be17",
            "data_session_id": "sim",
            "symbol": "TMFR1",
            "feature_snapshot": {"a": 1},
            "strategy_decision": {"r": "x"},
            "risk_decision": {},
            "order": {},
            "fills": [],
            "cancels": [],
            "pnl_after": None,
            "lifecycle_status": "canceled",
            "ts_emit": 1,
        }
        values = _extract_explanation_values(payload)
        assert values is not None
        col_idx = {name: i for i, name in enumerate(EXPLANATION_COLUMNS)}
        assert values[col_idx["trace_id"]] == "trace-dict"
        assert values[col_idx["lifecycle_status"]] == "canceled"

    def test_extract_returns_none_for_none_input(self) -> None:
        assert _extract_explanation_values(None) is None
