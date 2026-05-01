"""H3: order_id_map.jsonl ABA prevention via TTL + terminal-state schema.

Scenario the new schema closes:
1. Process A submits order with broker_id=42, persists ``{k:42, v:s1:1, ...}``.
2. Order completes (filled/cancelled). Terminal callback fires.
3. Process A crashes (or is restarted) — the persist file may already be
   on disk before the terminal callback flipped state to "terminal".
4. Broker re-uses broker_id=42 for an unrelated future order from
   strategy s2.
5. Process A restarts, loads the JSONL, and now misattributes the
   incoming fill on broker_id=42 to s1:1.

Without TTL or state tracking, step 5 lasts forever. With H3:
- Terminal entries are dropped on persist, so step 5's load skips them.
- Surviving entries past TTL (default 24h) are also dropped.
- The pre-H3 schema (``{k, v}`` only) is treated as terminal-stale and
  dropped on first load — old persisted state cannot resurrect.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from hft_platform.core import timebase
from hft_platform.order.adapter import OrderAdapter


def _make_adapter(persist_path: Path, *, ttl_s: float = 86400.0) -> OrderAdapter:
    """Build a bare OrderAdapter via __new__, seeding only the slots
    needed by load/persist/mark/helpers. Avoids the heavy load_config
    dependency on a yaml file."""
    adapter = OrderAdapter.__new__(OrderAdapter)
    adapter.order_id_map = {}
    adapter._order_id_map_lock = threading.RLock()
    adapter._order_id_map_max_size = 10000
    adapter._order_id_map_persist_path = str(persist_path)
    adapter._order_id_map_persist_interval_s = 1.0
    adapter._order_id_map_last_persist_s = 0.0
    adapter._order_id_meta = {}
    adapter._order_id_map_ttl_ns = int(ttl_s * 1_000_000_000)
    return adapter


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def test_new_schema_round_trip_preserves_t_ns_and_state(tmp_path: Path):
    persist = tmp_path / "order_id_map.jsonl"
    a1 = _make_adapter(persist)
    a1._set_order_id_mapping("BR42", "s1:1", source="test_seed")

    # Snapshot t_ns from in-memory metadata.
    seeded_t_ns, seeded_state = a1._order_id_meta["BR42"]
    assert seeded_state == "live"
    assert seeded_t_ns > 0

    a1.persist_order_id_map()
    rows = _read_jsonl(persist)
    assert len(rows) == 1
    assert rows[0] == {"k": "BR42", "v": "s1:1", "t_ns": seeded_t_ns, "s": "live"}

    # Fresh adapter loads the same file; metadata must round-trip.
    a2 = _make_adapter(persist)
    a2._load_order_id_map()
    assert a2.order_id_map == {"BR42": "s1:1"}
    assert a2._order_id_meta["BR42"] == (seeded_t_ns, "live")


def test_legacy_schema_is_dropped_on_load(tmp_path: Path):
    """Pre-H3 ``{k, v}`` rows have no creation timestamp or state.
    Resurrecting them is the ABA attack window — they must be skipped."""
    persist = tmp_path / "order_id_map.jsonl"
    persist.write_text('{"k":"BR1","v":"s1:1"}\n{"k":"BR2","v":"s2:2"}\n')
    adapter = _make_adapter(persist)
    adapter._load_order_id_map()
    assert adapter.order_id_map == {}, "legacy schema entries must be dropped on load to prevent ABA"


def test_ttl_expired_entries_dropped_on_load(tmp_path: Path):
    """Entries older than the TTL are skipped; fresh ones survive."""
    persist = tmp_path / "order_id_map.jsonl"
    now_ns = timebase.now_ns()
    # 25 hours old at 24h TTL — expired.
    stale_ns = now_ns - 25 * 3600 * 1_000_000_000
    fresh_ns = now_ns - 60 * 1_000_000_000  # 60 seconds old — fresh
    persist.write_text(
        json.dumps({"k": "STALE", "v": "s1:old", "t_ns": stale_ns, "s": "live"})
        + "\n"
        + json.dumps({"k": "FRESH", "v": "s2:new", "t_ns": fresh_ns, "s": "live"})
        + "\n"
    )
    adapter = _make_adapter(persist, ttl_s=86400.0)
    adapter._load_order_id_map()
    assert adapter.order_id_map == {"FRESH": "s2:new"}
    assert "STALE" not in adapter._order_id_meta


def test_terminal_state_entries_dropped_on_load(tmp_path: Path):
    persist = tmp_path / "order_id_map.jsonl"
    now_ns = timebase.now_ns()
    persist.write_text(
        json.dumps({"k": "T1", "v": "s1:done", "t_ns": now_ns, "s": "terminal"})
        + "\n"
        + json.dumps({"k": "L1", "v": "s2:live", "t_ns": now_ns, "s": "live"})
        + "\n"
    )
    adapter = _make_adapter(persist)
    adapter._load_order_id_map()
    assert adapter.order_id_map == {"L1": "s2:live"}


def test_mark_order_id_terminal_filters_persist(tmp_path: Path):
    """When `_mark_order_id_terminal` flags an entry, the next persist
    call drops it — terminal rows never leave RAM through the JSONL."""
    persist = tmp_path / "order_id_map.jsonl"
    adapter = _make_adapter(persist)
    adapter._set_order_id_mapping("BR1", "s1:done", source="test")
    adapter._set_order_id_mapping("BR2", "s2:live", source="test")

    # Order s1 reached terminal state — mark it.
    marked = adapter._mark_order_id_terminal("s1:done")
    assert marked == 1
    assert adapter._order_id_meta["BR1"][1] == "terminal"
    # In-memory map unchanged (downstream resolution still finds s1:done
    # for late callbacks until ``_del_order_id_mapping`` evicts it).
    assert adapter.order_id_map == {"BR1": "s1:done", "BR2": "s2:live"}

    adapter.persist_order_id_map()
    rows = _read_jsonl(persist)
    assert len(rows) == 1
    assert rows[0]["k"] == "BR2"
    assert rows[0]["s"] == "live"


def test_mark_order_id_terminal_idempotent(tmp_path: Path):
    persist = tmp_path / "order_id_map.jsonl"
    adapter = _make_adapter(persist)
    adapter._set_order_id_mapping("BR1", "s1:done", source="test")
    assert adapter._mark_order_id_terminal("s1:done") == 1
    # Second call is a no-op increment of count but state stays terminal.
    assert adapter._mark_order_id_terminal("s1:done") == 1
    assert adapter._order_id_meta["BR1"][1] == "terminal"


def test_unknown_order_key_marks_zero_entries(tmp_path: Path):
    persist = tmp_path / "order_id_map.jsonl"
    adapter = _make_adapter(persist)
    adapter._set_order_id_mapping("BR1", "s1:live", source="test")
    assert adapter._mark_order_id_terminal("unknown:99") == 0
    assert adapter._order_id_meta["BR1"][1] == "live"


def test_persist_writes_new_schema_keys(tmp_path: Path):
    """Validate the JSONL output schema: every line must contain k, v, t_ns, s."""
    persist = tmp_path / "order_id_map.jsonl"
    adapter = _make_adapter(persist)
    adapter._set_order_id_mapping("BR1", "s1:1", source="test")
    adapter.persist_order_id_map()
    rows = _read_jsonl(persist)
    assert len(rows) == 1
    row = rows[0]
    assert set(row.keys()) == {"k", "v", "t_ns", "s"}
    assert row["k"] == "BR1"
    assert row["v"] == "s1:1"
    assert row["s"] == "live"
    assert isinstance(row["t_ns"], int) and row["t_ns"] > 0


def test_load_skips_corrupt_lines(tmp_path: Path):
    """Loader is forgiving — malformed JSON lines must not crash startup."""
    persist = tmp_path / "order_id_map.jsonl"
    now_ns = timebase.now_ns()
    persist.write_text(
        "not json at all\n"
        + json.dumps({"k": "OK", "v": "s1:1", "t_ns": now_ns, "s": "live"})
        + "\n"
        + "{partial: bad\n"
    )
    adapter = _make_adapter(persist)
    adapter._load_order_id_map()
    assert adapter.order_id_map == {"OK": "s1:1"}


def test_zero_ttl_disables_age_filter(tmp_path: Path):
    """``HFT_ORDER_ID_MAP_TTL_S=0`` should disable age-based eviction.
    Useful for ops emergencies where the persist file is the source of
    truth for active orders. Only state filter (terminal) still applies."""
    persist = tmp_path / "order_id_map.jsonl"
    very_old_ns = timebase.now_ns() - 365 * 24 * 3600 * 1_000_000_000  # 1 year old
    persist.write_text(json.dumps({"k": "ANCIENT", "v": "s1:old", "t_ns": very_old_ns, "s": "live"}) + "\n")
    adapter = _make_adapter(persist, ttl_s=0.0)
    adapter._load_order_id_map()
    assert adapter.order_id_map == {"ANCIENT": "s1:old"}


def test_round_trip_after_terminal_filter(tmp_path: Path):
    """End-to-end: write 2 entries, terminate one, persist, reload — only
    the live entry survives. The terminated id cannot resurrect."""
    persist = tmp_path / "order_id_map.jsonl"
    a1 = _make_adapter(persist)
    a1._set_order_id_mapping("BR1", "s1:1", source="seed")
    a1._set_order_id_mapping("BR2", "s2:2", source="seed")
    a1._mark_order_id_terminal("s1:1")
    a1.persist_order_id_map()

    a2 = _make_adapter(persist)
    a2._load_order_id_map()
    assert a2.order_id_map == {"BR2": "s2:2"}
    assert a2._order_id_meta["BR2"][1] == "live"


def test_register_broker_ids_bulk_stamps_metadata(tmp_path: Path):
    """``register_broker_ids_bulk`` (external batch entry point) must
    produce the same metadata stamping as the per-key helper."""
    persist = tmp_path / "order_id_map.jsonl"
    adapter = _make_adapter(persist)
    changed = adapter.register_broker_ids_bulk(["B1", "B2", "B3"], "s9:42")
    assert changed is True
    for token in ("B1", "B2", "B3"):
        assert adapter.order_id_map[token] == "s9:42"
        assert adapter._order_id_meta[token][1] == "live"
        assert adapter._order_id_meta[token][0] > 0


@pytest.mark.parametrize("legacy_count", [1, 5, 25])
def test_legacy_load_metric_skipped_legacy_count(tmp_path: Path, legacy_count: int):
    """Pre-H3 entries are counted into ``skipped_legacy`` (logged, not metric).
    This test just verifies the loader does not raise and dropped count is
    consistent — important so the upgrade path is observable in production."""
    persist = tmp_path / "order_id_map.jsonl"
    persist.write_text("\n".join(json.dumps({"k": f"LEG{i}", "v": f"s:{i}"}) for i in range(legacy_count)) + "\n")
    adapter = _make_adapter(persist)
    adapter._load_order_id_map()
    assert len(adapter.order_id_map) == 0
