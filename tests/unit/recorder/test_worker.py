"""Unit tests for ``hft_platform.recorder.worker`` — Slice C task 3 additions.

Covers the opt-in ``intents`` topic added by Slice C task 3:
- topic disabled by default (env var unset)
- topic registered with correct table when ``HFT_INTENT_RECORDER_ENABLED=1``
- ``_extract_intent_values`` round-trips the OrderIntent fields the
  ``hft.order_intents`` schema persists.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.recorder import worker as worker_mod


def _make_intent() -> OrderIntent:
    return OrderIntent(
        intent_id=4242,
        strategy_id="r47_maker",
        symbol="TMFD6",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=171_950_000,  # scaled int x10000
        qty=1,
        tif=TIF.LIMIT,
        target_order_id=None,
        timestamp_ns=1_700_000_000_000_000_000,
        source_ts_ns=1_700_000_000_000_000_500,
        reason="test_round_trip",
        trace_id="trace-abc",
        idempotency_key="idem-xyz",
        ttl_ns=5_000_000_000,
        decision_price=171_945_000,
        price_type="LMT",
    )


class TestIntentTopicRegistration(unittest.TestCase):
    """Topic gating by ``HFT_INTENT_RECORDER_ENABLED`` (default 0)."""

    def setUp(self) -> None:
        # Disable ClickHouse so RecorderService.__init__ does not open a network
        # connection when the writer is constructed. We only exercise __init__.
        self._env = patch.dict(os.environ, {"HFT_CLICKHOUSE_ENABLED": "0"}, clear=False)
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()

    def test_intent_topic_disabled_by_default(self) -> None:
        """With env var unset, ``intents`` MUST NOT appear in svc.batchers."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HFT_INTENT_RECORDER_ENABLED", None)
            queue: asyncio.Queue = asyncio.Queue()
            svc = worker_mod.RecorderService(queue)
        self.assertNotIn("intents", svc.batchers)

    def test_intent_topic_registered_when_enabled(self) -> None:
        """With env var = '1', ``intents`` batcher MUST exist with the
        ``hft.order_intents`` table name."""
        with patch.dict(os.environ, {"HFT_INTENT_RECORDER_ENABLED": "1"}, clear=False):
            queue: asyncio.Queue = asyncio.Queue()
            svc = worker_mod.RecorderService(queue)
        self.assertIn("intents", svc.batchers)
        self.assertEqual(svc.batchers["intents"].table_name, "hft.order_intents")


class TestExtractIntentValuesRoundTrip(unittest.TestCase):
    """Verify ``_extract_intent_values`` preserves every persisted field."""

    def test_extract_intent_values_round_trip(self) -> None:
        intent = _make_intent()
        ingest_ts = 1_700_000_000_000_000_999

        envelope = {"intent": intent, "ingest_ts": ingest_ts}
        values = worker_mod._extract_intent_values(envelope)
        self.assertIsNotNone(values)
        # One value per declared column.
        self.assertEqual(len(values), len(worker_mod.INTENT_COLUMNS))

        as_dict = dict(zip(worker_mod.INTENT_COLUMNS, values))

        self.assertEqual(as_dict["intent_id"], intent.intent_id)
        self.assertEqual(as_dict["strategy_id"], intent.strategy_id)
        self.assertEqual(as_dict["symbol"], intent.symbol)
        self.assertEqual(as_dict["intent_type"], intent.intent_type.name)
        self.assertEqual(as_dict["side"], intent.side.name)
        self.assertEqual(as_dict["price_scaled"], intent.price)
        self.assertEqual(as_dict["qty"], intent.qty)
        self.assertEqual(as_dict["tif"], intent.tif.name)
        # target_order_id None → empty string for ClickHouse String column.
        self.assertEqual(as_dict["target_order_id"], "")
        self.assertEqual(as_dict["timestamp_ns"], intent.timestamp_ns)
        self.assertEqual(as_dict["source_ts_ns"], intent.source_ts_ns)
        self.assertEqual(as_dict["decision_price"], intent.decision_price)
        self.assertEqual(as_dict["price_type"], intent.price_type)
        self.assertEqual(as_dict["trace_id"], intent.trace_id)
        self.assertEqual(as_dict["idempotency_key"], intent.idempotency_key)
        self.assertEqual(as_dict["ttl_ns"], intent.ttl_ns)
        self.assertEqual(as_dict["reason"], intent.reason)
        self.assertEqual(as_dict["ingest_ts"], ingest_ts)

    def test_extract_intent_values_returns_none_for_none_row(self) -> None:
        self.assertIsNone(worker_mod._extract_intent_values(None))

    def test_extract_intent_values_returns_none_when_intent_missing(self) -> None:
        self.assertIsNone(worker_mod._extract_intent_values({"ingest_ts": 1}))


if __name__ == "__main__":
    unittest.main()
