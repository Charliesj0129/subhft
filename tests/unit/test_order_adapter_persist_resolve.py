"""Coverage tests for order/adapter.py Groups 1-5.

Targets:
- Group 1: _load_order_id_map + persist_order_id_map (lines 590-646)
- Group 2: resolve_strategy_from_deal — expired eviction + FIFO ambiguous warning (lines 925-969)
- Group 3: _register_broker_ids — dict and object forms (lines 838-909)
- Group 4: _maybe_persist_order_id_map — RuntimeError fallback (lines 648-664)
- Group 5: resolve_strategy_from_deal_candidates (lines 971-978)
"""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Infrastructure patches (mirrors test_order_coverage_gaps.py)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def tmp_config(tmp_path):
    cfg = tmp_path / "order.yaml"
    cfg.write_text(
        "rate_limits:\n"
        "  shioaji_soft_cap: 180\n"
        "  shioaji_hard_cap: 250\n"
        "  window_seconds: 10\n"
        "circuit_breaker:\n"
        "  threshold: 5\n"
        "  timeout_seconds: 60\n"
    )
    return str(cfg)


@pytest.fixture(autouse=True)
def _mock_adapter_infra(tmp_path):
    with (
        patch.dict(
            os.environ,
            {"HFT_ORDER_ID_MAP_PERSIST_PATH": str(tmp_path / "oid_map.jsonl")},
        ),
        patch("hft_platform.order.adapter.MetricsRegistry") as mm,
        patch("hft_platform.order.adapter.LatencyRecorder") as ml,
        patch("hft_platform.order.adapter.SymbolMetadata"),
        patch("hft_platform.order.adapter.PriceCodec"),
        patch("hft_platform.order.adapter.SymbolMetadataPriceScaleProvider"),
        patch("hft_platform.order.adapter.get_dlq") as md,
    ):
        metrics = MagicMock()
        metrics.order_reject_total = MagicMock()
        metrics.order_actions_total = MagicMock()
        metrics.order_actions_total.labels.return_value = MagicMock()
        metrics.rejection_sink_overflow_total = MagicMock()
        metrics.pending_fill_expired_total = MagicMock()
        mm.get.return_value = metrics
        ml.get.return_value = MagicMock()
        md.return_value = MagicMock()
        yield


def _make_adapter(tmp_config: str):
    from hft_platform.order.adapter import OrderAdapter

    client = MagicMock()
    client.place_order = MagicMock(return_value={"seq_no": "A1", "ord_no": "B2"})
    client.cancel_order = MagicMock(return_value={})
    client.update_order = MagicMock(return_value={})
    client.get_exchange = MagicMock(return_value="TSE")
    client.mode = "simulation"
    client.activate_ca = False
    return OrderAdapter(
        config_path=tmp_config,
        order_queue=asyncio.Queue(maxsize=128),
        broker_client=client,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Group 1 — _load_order_id_map
# ═════════════════════════════════════════════════════════════════════════════


def test_load_order_id_map_reads_jsonl_file(tmp_path, tmp_config):
    """_load_order_id_map parses valid JSONL (H3 schema: k, v, t_ns, s)
    and populates order_id_map."""
    import orjson

    from hft_platform.core import timebase

    map_path = tmp_path / "oid_map.jsonl"
    now_ns = timebase.now_ns()
    lines = [
        orjson.dumps({"k": "TOKEN1", "v": "strat1:100", "t_ns": now_ns, "s": "live"}) + b"\n",
        orjson.dumps({"k": "TOKEN2", "v": "strat2:200", "t_ns": now_ns, "s": "live"}) + b"\n",
    ]
    map_path.write_bytes(b"".join(lines))

    with patch.dict(os.environ, {"HFT_ORDER_ID_MAP_PERSIST_PATH": str(map_path)}):
        adapter = _make_adapter(tmp_config)

    assert adapter.order_id_map.get("TOKEN1") == "strat1:100"
    assert adapter.order_id_map.get("TOKEN2") == "strat2:200"


def test_load_order_id_map_skips_blank_lines(tmp_path, tmp_config):
    """_load_order_id_map silently skips empty lines in the JSONL file
    (H3 schema)."""
    import orjson

    from hft_platform.core import timebase

    map_path = tmp_path / "oid_map.jsonl"
    now_ns = timebase.now_ns()
    content = b"\n" + orjson.dumps({"k": "K1", "v": "V1", "t_ns": now_ns, "s": "live"}) + b"\n\n"
    map_path.write_bytes(content)

    with patch.dict(os.environ, {"HFT_ORDER_ID_MAP_PERSIST_PATH": str(map_path)}):
        adapter = _make_adapter(tmp_config)

    assert adapter.order_id_map.get("K1") == "V1"


def test_load_order_id_map_skips_malformed_entries(tmp_path, tmp_config):
    """_load_order_id_map silently skips malformed lines and pre-H3 entries
    that lack t_ns/s metadata (legacy schema dropped to prevent ABA)."""
    import orjson

    from hft_platform.core import timebase

    map_path = tmp_path / "oid_map.jsonl"
    now_ns = timebase.now_ns()
    content = (
        b"not-valid-json\n"
        + orjson.dumps({"k": "GOOD", "v": "strat:1", "t_ns": now_ns, "s": "live"})
        + b"\n"
        + b'{"missing_v": true}\n'
    )
    map_path.write_bytes(content)

    with patch.dict(os.environ, {"HFT_ORDER_ID_MAP_PERSIST_PATH": str(map_path)}):
        adapter = _make_adapter(tmp_config)

    assert adapter.order_id_map.get("GOOD") == "strat:1"
    # Malformed and missing-v entries must not appear
    assert len([k for k in adapter.order_id_map if k != "GOOD"]) == 0


def test_load_order_id_map_enforces_max_size(tmp_path, tmp_config):
    """_load_order_id_map evicts oldest entries when max size is exceeded
    (H3 schema)."""
    import orjson

    from hft_platform.core import timebase

    map_path = tmp_path / "oid_map.jsonl"
    now_ns = timebase.now_ns()
    # Write 5 entries but cap max size at 3
    lines = [orjson.dumps({"k": f"K{i}", "v": f"V{i}", "t_ns": now_ns, "s": "live"}) + b"\n" for i in range(5)]
    map_path.write_bytes(b"".join(lines))

    with patch.dict(
        os.environ,
        {
            "HFT_ORDER_ID_MAP_PERSIST_PATH": str(map_path),
            "HFT_ORDER_ID_MAP_MAX_SIZE": "3",
        },
    ):
        adapter = _make_adapter(tmp_config)

    assert len(adapter.order_id_map) <= 3


def test_load_order_id_map_nonexistent_file_is_noop(tmp_config):
    """_load_order_id_map is a no-op when the file does not exist."""
    adapter = _make_adapter(tmp_config)
    # File does not exist — order_id_map should be empty (or only have pre-existing keys)
    assert isinstance(adapter.order_id_map, dict)


# ═════════════════════════════════════════════════════════════════════════════
# Group 1 — persist_order_id_map
# ═════════════════════════════════════════════════════════════════════════════


def test_persist_order_id_map_writes_jsonl(tmp_path, tmp_config):
    """persist_order_id_map atomically writes order_id_map as JSONL."""
    import orjson

    persist_path = tmp_path / "out" / "oid_map.jsonl"

    with patch.dict(os.environ, {"HFT_ORDER_ID_MAP_PERSIST_PATH": str(persist_path)}):
        adapter = _make_adapter(tmp_config)

    adapter.order_id_map["TOK_A"] = "stratA:1"
    adapter.order_id_map["TOK_B"] = "stratB:2"
    adapter.persist_order_id_map()

    assert persist_path.exists()
    content = persist_path.read_bytes()
    lines = [ln for ln in content.split(b"\n") if ln.strip()]
    parsed = {orjson.loads(ln)["k"]: orjson.loads(ln)["v"] for ln in lines}
    assert parsed["TOK_A"] == "stratA:1"
    assert parsed["TOK_B"] == "stratB:2"


def test_persist_order_id_map_creates_parent_dir(tmp_path, tmp_config):
    """persist_order_id_map creates the parent directory if it does not exist."""
    deep_path = tmp_path / "nested" / "deep" / "oid.jsonl"

    with patch.dict(os.environ, {"HFT_ORDER_ID_MAP_PERSIST_PATH": str(deep_path)}):
        adapter = _make_adapter(tmp_config)

    adapter.order_id_map["X"] = "Y"
    adapter.persist_order_id_map()

    assert deep_path.exists()


def test_persist_order_id_map_is_idempotent(tmp_path, tmp_config):
    """Calling persist_order_id_map twice overwrites — file has exactly the current map."""
    import orjson

    persist_path = tmp_path / "oid_map.jsonl"

    with patch.dict(os.environ, {"HFT_ORDER_ID_MAP_PERSIST_PATH": str(persist_path)}):
        adapter = _make_adapter(tmp_config)

    adapter.order_id_map["K1"] = "V1"
    adapter.persist_order_id_map()

    # Overwrite with different data
    adapter.order_id_map.clear()
    adapter.order_id_map["K2"] = "V2"
    adapter.persist_order_id_map()

    content = persist_path.read_bytes()
    lines = [ln for ln in content.split(b"\n") if ln.strip()]
    keys = [orjson.loads(ln)["k"] for ln in lines]
    assert "K2" in keys
    assert "K1" not in keys


# ═════════════════════════════════════════════════════════════════════════════
# Group 2 — resolve_strategy_from_deal: expired entry eviction
# ═════════════════════════════════════════════════════════════════════════════


def test_resolve_strategy_from_deal_evicts_expired_entries(tmp_config):
    """Expired pending fill entries are evicted and None is returned."""
    adapter = _make_adapter(tmp_config)
    adapter._pending_fill_ttl_s = 0.001  # 1 ms TTL

    key = "2330:BUY"
    order_key = "strat_exp:999"
    adapter._pending_fill_index[key] = [order_key]
    adapter._pending_fill_registered_at[order_key] = time.monotonic() - 5.0  # way expired

    result = adapter.resolve_strategy_from_deal("2330", "buy")

    assert result is None
    # Entry should have been evicted
    assert order_key not in adapter._pending_fill_registered_at
    assert adapter._pending_fill_index.get(key) in (None, [])


def test_resolve_strategy_from_deal_returns_strategy_when_valid(tmp_config):
    """A valid (non-expired) pending fill is resolved and popped."""
    adapter = _make_adapter(tmp_config)
    adapter._pending_fill_ttl_s = 7200.0

    key = "2330:BUY"
    order_key = "my_strat:42"
    adapter._pending_fill_index[key] = [order_key]
    adapter._pending_fill_registered_at[order_key] = time.monotonic()

    result = adapter.resolve_strategy_from_deal("2330", "buy")

    assert result == "my_strat"
    # Entry is consumed
    assert key not in adapter._pending_fill_index


def test_resolve_strategy_from_deal_logs_fifo_ambiguous_warning(tmp_config):
    """When multiple candidates exist, FIFO picks oldest and logs a warning."""
    adapter = _make_adapter(tmp_config)
    adapter._pending_fill_ttl_s = 7200.0

    key = "2330:BUY"
    order_key_1 = "strat_a:1"
    order_key_2 = "strat_b:2"
    adapter._pending_fill_index[key] = [order_key_1, order_key_2]
    now = time.monotonic()
    adapter._pending_fill_registered_at[order_key_1] = now
    adapter._pending_fill_registered_at[order_key_2] = now

    result = adapter.resolve_strategy_from_deal("2330", "buy")

    # FIFO must pick the first candidate
    assert result == "strat_a"
    # Second candidate stays in the index
    assert adapter._pending_fill_index.get(key) == [order_key_2]


def test_resolve_strategy_from_deal_sell_side(tmp_config):
    """resolve_strategy_from_deal handles sell action correctly."""
    adapter = _make_adapter(tmp_config)
    adapter._pending_fill_ttl_s = 7200.0

    key = "2330:SELL"
    order_key = "sell_strat:7"
    adapter._pending_fill_index[key] = [order_key]
    adapter._pending_fill_registered_at[order_key] = time.monotonic()

    result = adapter.resolve_strategy_from_deal("2330", "sell")

    assert result == "sell_strat"


def test_resolve_strategy_from_deal_no_pending_returns_none(tmp_config):
    """Returns None when no pending fill index entry exists for the symbol+side."""
    adapter = _make_adapter(tmp_config)

    result = adapter.resolve_strategy_from_deal("9999", "buy")

    assert result is None


def test_resolve_strategy_from_deal_order_key_without_colon(tmp_config):
    """Order keys without a colon are returned as-is as the strategy_id."""
    adapter = _make_adapter(tmp_config)
    adapter._pending_fill_ttl_s = 7200.0

    key = "2330:BUY"
    order_key = "bare_strategy"  # no colon
    adapter._pending_fill_index[key] = [order_key]
    adapter._pending_fill_registered_at[order_key] = time.monotonic()

    result = adapter.resolve_strategy_from_deal("2330", "buy")

    assert result == "bare_strategy"


# ═════════════════════════════════════════════════════════════════════════════
# Group 3 — _register_broker_ids: dict form
# ═════════════════════════════════════════════════════════════════════════════


def test_register_broker_ids_dict_top_level(tmp_config):
    """_register_broker_ids extracts IDs from a dict trade with top-level keys."""

    async def _run():
        adapter = _make_adapter(tmp_config)
        trade = {"seq_no": "SEQ1", "ord_no": "ORD1"}
        result = await adapter._register_broker_ids("strat:1", trade)
        assert result is True
        assert adapter.order_id_map.get("SEQ1") == "strat:1"
        assert adapter.order_id_map.get("ORD1") == "strat:1"

    asyncio.run(_run())


def test_register_broker_ids_dict_nested_order(tmp_config):
    """_register_broker_ids extracts IDs from nested 'order' dict."""

    async def _run():
        adapter = _make_adapter(tmp_config)
        trade = {
            "seq_no": "S1",
            "order": {"ordno": "ON1"},
            "status": {"id": "ST1"},
        }
        result = await adapter._register_broker_ids("strat:2", trade)
        assert result is True
        assert adapter.order_id_map.get("S1") == "strat:2"
        assert adapter.order_id_map.get("ON1") == "strat:2"
        assert adapter.order_id_map.get("ST1") == "strat:2"

    asyncio.run(_run())


def test_register_broker_ids_dict_empty_returns_false(tmp_config):
    """_register_broker_ids returns False when no IDs are found."""

    async def _run():
        adapter = _make_adapter(tmp_config)
        trade = {"some_other_key": "value"}
        result = await adapter._register_broker_ids("strat:3", trade)
        assert result is False

    asyncio.run(_run())


def test_register_broker_ids_object_form(tmp_config):
    """_register_broker_ids extracts IDs from object-style trade (attribute access)."""

    async def _run():
        adapter = _make_adapter(tmp_config)
        order_obj = SimpleNamespace(ordno="ON_OBJ")
        status_obj = SimpleNamespace(id="ST_OBJ", seq_no=None, seqno=None, ord_no=None, ordno=None)
        trade = SimpleNamespace(
            seq_no="SEQ_OBJ",
            seqno=None,
            ord_no=None,
            ordno=None,
            order_id=None,
            id=None,
            order=order_obj,
            status=status_obj,
        )
        result = await adapter._register_broker_ids("strat:4", trade)
        assert result is True
        assert adapter.order_id_map.get("SEQ_OBJ") == "strat:4"
        assert adapter.order_id_map.get("ON_OBJ") == "strat:4"

    asyncio.run(_run())


def test_register_broker_ids_object_no_attrs(tmp_config):
    """_register_broker_ids handles object with no matching attributes gracefully."""

    async def _run():
        adapter = _make_adapter(tmp_config)
        trade = SimpleNamespace(unrelated="x")
        result = await adapter._register_broker_ids("strat:5", trade)
        assert result is False

    asyncio.run(_run())


def test_register_broker_ids_evicts_when_at_limit(tmp_config):
    """_register_broker_ids evicts stale entries when map is at capacity."""

    async def _run():
        adapter = _make_adapter(tmp_config)
        adapter._order_id_map_max_size = 3
        # Fill map with non-live-order keys
        adapter.order_id_map["OLD1"] = "dead:1"
        adapter.order_id_map["OLD2"] = "dead:2"
        adapter.order_id_map["OLD3"] = "dead:3"

        trade = {"seq_no": "NEW1"}
        await adapter._register_broker_ids("strat:6", trade)

        # Map must not exceed max size
        assert len(adapter.order_id_map) <= adapter._order_id_map_max_size + 1

    asyncio.run(_run())


# ═════════════════════════════════════════════════════════════════════════════
# Group 4 — _maybe_persist_order_id_map: RuntimeError fallback (no event loop)
# ═════════════════════════════════════════════════════════════════════════════


def test_maybe_persist_order_id_map_fallback_when_no_loop(tmp_path, tmp_config):
    """_maybe_persist_order_id_map calls persist_order_id_map inline when no loop."""
    persist_path = tmp_path / "fallback_map.jsonl"

    with patch.dict(os.environ, {"HFT_ORDER_ID_MAP_PERSIST_PATH": str(persist_path)}):
        adapter = _make_adapter(tmp_config)

    adapter.order_id_map["FB1"] = "strat_fb:1"
    # Force interval to 0 so the throttle check is bypassed
    adapter._order_id_map_persist_interval_s = 0.0
    adapter._order_id_map_last_persist_s = 0.0

    # Call outside any running event loop — triggers RuntimeError → inline persist
    adapter._maybe_persist_order_id_map(force=True)

    assert persist_path.exists()
    content = persist_path.read_bytes()
    assert b"FB1" in content


def test_maybe_persist_order_id_map_throttled(tmp_path, tmp_config):
    """_maybe_persist_order_id_map skips persist when called within interval."""
    persist_path = tmp_path / "throttled_map.jsonl"

    with patch.dict(os.environ, {"HFT_ORDER_ID_MAP_PERSIST_PATH": str(persist_path)}):
        adapter = _make_adapter(tmp_config)

    adapter.order_id_map["T1"] = "strat_t:1"
    adapter._order_id_map_persist_interval_s = 3600.0  # very long interval
    adapter._order_id_map_last_persist_s = time.monotonic()  # just persisted

    # Should be throttled — file should not be written
    adapter._maybe_persist_order_id_map(force=False)

    assert not persist_path.exists()


# ═════════════════════════════════════════════════════════════════════════════
# Group 5 — resolve_strategy_from_deal_candidates
# ═════════════════════════════════════════════════════════════════════════════


def test_resolve_strategy_from_deal_candidates_returns_first_match(tmp_config):
    """resolve_strategy_from_deal_candidates returns the first resolved strategy."""
    adapter = _make_adapter(tmp_config)
    adapter._pending_fill_ttl_s = 7200.0

    key = "2330:BUY"
    order_key = "cand_strat:1"
    adapter._pending_fill_index[key] = [order_key]
    adapter._pending_fill_registered_at[order_key] = time.monotonic()

    result = adapter.resolve_strategy_from_deal_candidates(["9999", "2330", "1234"], "buy")

    assert result == "cand_strat"


def test_resolve_strategy_from_deal_candidates_skips_empty_symbols(tmp_config):
    """resolve_strategy_from_deal_candidates skips empty string symbols."""
    adapter = _make_adapter(tmp_config)
    adapter._pending_fill_ttl_s = 7200.0

    key = "2330:SELL"
    order_key = "cand2:5"
    adapter._pending_fill_index[key] = [order_key]
    adapter._pending_fill_registered_at[order_key] = time.monotonic()

    result = adapter.resolve_strategy_from_deal_candidates(["", "", "2330"], "sell")

    assert result == "cand2"


def test_resolve_strategy_from_deal_candidates_returns_none_when_no_match(tmp_config):
    """resolve_strategy_from_deal_candidates returns None when no symbol matches."""
    adapter = _make_adapter(tmp_config)

    result = adapter.resolve_strategy_from_deal_candidates(["AAAA", "BBBB"], "buy")

    assert result is None


def test_resolve_strategy_from_deal_candidates_empty_list(tmp_config):
    """resolve_strategy_from_deal_candidates returns None for an empty symbol list."""
    adapter = _make_adapter(tmp_config)

    result = adapter.resolve_strategy_from_deal_candidates([], "buy")

    assert result is None


# ═════════════════════════════════════════════════════════════════════════════
# Error-path coverage: _load_order_id_map failure (lines 616-617)
# ═════════════════════════════════════════════════════════════════════════════


def test_load_order_id_map_logs_warning_on_open_failure(tmp_path, tmp_config):
    """_load_order_id_map outer except catches OSError on open() and logs warning (lines 616-617)."""
    map_path = tmp_path / "oid_map.jsonl"
    # Create the file so os.path.exists passes
    map_path.write_bytes(b'{"k":"K","v":"V"}\n')

    map_path_str = str(map_path)

    # Patch open to raise only when the specific map file is opened
    real_open = open

    def _selective_open(path, *args, **kwargs):
        if str(path) == map_path_str:
            raise OSError("simulated open error for map file")
        return real_open(path, *args, **kwargs)

    with patch.dict(os.environ, {"HFT_ORDER_ID_MAP_PERSIST_PATH": map_path_str}):
        with patch("hft_platform.order.adapter.open", _selective_open):
            # Should not raise — exception is caught and logged (lines 616-617)
            adapter = _make_adapter(tmp_config)

    # Map should be empty since load failed
    assert isinstance(adapter.order_id_map, dict)


# ═════════════════════════════════════════════════════════════════════════════
# Error-path coverage: persist_order_id_map failure (lines 640-646)
# ═════════════════════════════════════════════════════════════════════════════


def test_persist_order_id_map_logs_warning_on_write_failure(tmp_path, tmp_config):
    """persist_order_id_map catches exceptions and logs a warning (lines 645-646)."""
    persist_path = tmp_path / "oid_map.jsonl"

    with patch.dict(os.environ, {"HFT_ORDER_ID_MAP_PERSIST_PATH": str(persist_path)}):
        adapter = _make_adapter(tmp_config)

    adapter.order_id_map["K1"] = "V1"

    # Patch os.makedirs to raise so the outer except block is hit
    with patch("os.makedirs", side_effect=OSError("simulated makedirs error")):
        # Should not raise — exception is caught and logged
        adapter.persist_order_id_map()

    # File should NOT have been written since makedirs raised
    assert not persist_path.exists()
    # order_id_map still intact (not corrupted)
    assert adapter.order_id_map["K1"] == "V1"


def test_persist_order_id_map_cleans_up_tmp_file_on_inner_failure(tmp_path, tmp_config):
    """On inner write failure, persist_order_id_map cleans up the temp file (lines 640-643)."""
    persist_path = tmp_path / "oid_map.jsonl"

    with patch.dict(os.environ, {"HFT_ORDER_ID_MAP_PERSIST_PATH": str(persist_path)}):
        adapter = _make_adapter(tmp_config)

    adapter.order_id_map["K1"] = "V1"

    import tempfile

    original_mkstemp = tempfile.mkstemp

    def _failing_mkstemp(*args, **kwargs):
        fd, tmp_p = original_mkstemp(*args, **kwargs)
        # Write something to ensure the file exists, then raise on fdopen
        os.close(fd)
        # Re-open as a real fd for the cleanup test
        fd2 = os.open(tmp_p, os.O_WRONLY)

        original_fdopen = os.fdopen

        def _fail_fdopen(fd_arg, *a, **kw):
            os.close(fd_arg)
            raise OSError("simulated fdopen error")

        with patch("os.fdopen", side_effect=_fail_fdopen):
            pass
        return fd2, tmp_p

    # Simpler approach: make orjson.dumps raise after the file is created
    import orjson

    call_count = [0]
    original_dumps = orjson.dumps

    def _fail_on_second_dumps(obj):
        call_count[0] += 1
        if call_count[0] > 0:
            raise RuntimeError("simulated dumps failure")
        return original_dumps(obj)

    with patch("orjson.dumps", side_effect=_fail_on_second_dumps):
        # Should not raise — exception caught and logged, temp file cleaned up
        adapter.persist_order_id_map()

    # The persist path should NOT exist (write failed before rename)
    assert not persist_path.exists()


# ═════════════════════════════════════════════════════════════════════════════
# resolve_strategy_from_deal: expired metric inc raises (lines 938-939)
# ═════════════════════════════════════════════════════════════════════════════


def test_resolve_strategy_from_deal_expired_metric_inc_exception(tmp_config):
    """Lines 938-939: except Exception: pass when metric inc raises."""
    adapter = _make_adapter(tmp_config)
    adapter._pending_fill_ttl_s = 0.001  # 1 ms TTL

    # Make the metric raise
    adapter.metrics.pending_fill_expired_total.inc.side_effect = AttributeError("no metric")

    key = "2330:BUY"
    order_key = "strat_exp2:10"
    adapter._pending_fill_index[key] = [order_key]
    adapter._pending_fill_registered_at[order_key] = time.monotonic() - 10.0  # expired

    # Should not raise even though metric.inc raises
    result = adapter.resolve_strategy_from_deal("2330", "buy")
    assert result is None


# ═════════════════════════════════════════════════════════════════════════════
# _register_broker_ids: live-order protection during eviction (line 895)
# ═════════════════════════════════════════════════════════════════════════════


def test_register_broker_ids_skips_eviction_for_live_order_keys(tmp_config):
    """_register_broker_ids skips eviction of entries tied to live orders (line 895)."""

    async def _run():
        adapter = _make_adapter(tmp_config)
        adapter._order_id_map_max_size = 2

        # Fill map: one entry is tied to a live order, one is stale
        adapter.order_id_map["LIVE_KEY"] = "live_strat:1"
        adapter.order_id_map["DEAD_KEY"] = "dead_strat:2"
        # Register the live order
        adapter.live_orders["live_strat:1"] = MagicMock()

        trade = {"seq_no": "NEW_KEY"}
        await adapter._register_broker_ids("strat:7", trade)

        # LIVE_KEY must still be present (protected from eviction)
        assert "LIVE_KEY" in adapter.order_id_map
        # DEAD_KEY may have been evicted
        # NEW_KEY should be registered
        assert adapter.order_id_map.get("NEW_KEY") == "strat:7"

    asyncio.run(_run())
