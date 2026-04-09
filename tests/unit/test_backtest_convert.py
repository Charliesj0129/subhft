import json

import numpy as np
import pytest

from hft_platform.backtest import convert as conv

# ---------------------------------------------------------------------------
# Shared dtype fixture
# ---------------------------------------------------------------------------
_EVENT_DTYPE = np.dtype(
    [
        ("ev", "i4"),
        ("exch", "i8"),
        ("local", "i8"),
        ("px", "f8"),
        ("qty", "f8"),
        ("r1", "i8"),
        ("r2", "i8"),
        ("r3", "f8"),
    ]
)


@pytest.fixture()
def patched_conv(monkeypatch):
    """Monkeypatch conv module with stub hftbacktest constants."""
    monkeypatch.setattr(conv, "event_dtype", _EVENT_DTYPE)
    monkeypatch.setattr(conv, "DEPTH_EVENT", 1)
    monkeypatch.setattr(conv, "TRADE_EVENT", 2)
    monkeypatch.setattr(conv, "EXCH_EVENT", 4)
    monkeypatch.setattr(conv, "LOCAL_EVENT", 8)
    monkeypatch.setattr(conv, "BUY_EVENT", 16)
    monkeypatch.setattr(conv, "SELL_EVENT", 32)
    monkeypatch.setattr(conv, "_import_error", None)
    return conv


# ---------------------------------------------------------------------------
# Original test (kept intact)
# ---------------------------------------------------------------------------


def test_convert_jsonl_to_npz(tmp_path, monkeypatch):
    monkeypatch.setattr(conv, "event_dtype", _EVENT_DTYPE)
    monkeypatch.setattr(conv, "DEPTH_EVENT", 1)
    monkeypatch.setattr(conv, "TRADE_EVENT", 2)
    monkeypatch.setattr(conv, "EXCH_EVENT", 4)
    monkeypatch.setattr(conv, "LOCAL_EVENT", 8)
    monkeypatch.setattr(conv, "BUY_EVENT", 16)
    monkeypatch.setattr(conv, "SELL_EVENT", 32)

    input_path = tmp_path / "events.jsonl"
    output_path = tmp_path / "out.npz"

    rows = [
        {
            "type": "BidAsk",
            "ts": 100,
            "bids": [{"price": 10000, "volume": 1}],
            "asks": [{"price": 11000, "volume": 2}],
        },
        {
            "type": "Tick",
            "ts": 200,
            "price": 123400,
            "volume": 3,
        },
    ]
    input_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    conv.convert_jsonl_to_npz(str(input_path), str(output_path), scale=10000)

    data = np.load(output_path)["data"]
    assert data.shape[0] == 3
    prices = sorted(round(float(p), 3) for p in data["px"])
    assert prices == [1.0, 1.1, 12.34]


# ---------------------------------------------------------------------------
# _build_event helper
# ---------------------------------------------------------------------------


class TestBuildEvent:
    def test_returns_eight_tuple(self):
        result = conv._build_event(5, 1000, 2000, 1.23, 10.0)
        assert len(result) == 8

    def test_event_code_preserved(self):
        result = conv._build_event(7, 0, 0, 0.0, 0.0)
        assert result[0] == 7

    def test_timestamps_cast_to_int(self):
        result = conv._build_event(1, 1.9, 2.1, 0.0, 0.0)
        assert isinstance(result[1], int)
        assert isinstance(result[2], int)
        assert result[1] == 1
        assert result[2] == 2

    def test_price_and_qty_cast_to_float(self):
        result = conv._build_event(1, 0, 0, 3, 7)
        assert isinstance(result[3], float)
        assert isinstance(result[4], float)
        assert result[3] == 3.0
        assert result[4] == 7.0

    def test_trailing_fields_are_zeros(self):
        result = conv._build_event(0, 0, 0, 0.0, 0.0)
        assert result[5] == 0
        assert result[6] == 0
        assert result[7] == 0.0


# ---------------------------------------------------------------------------
# convert_jsonl_to_npz — hftbacktest not installed guard
# ---------------------------------------------------------------------------


class TestConvertHftbacktestNotInstalled:
    def test_raises_runtime_error_when_event_dtype_none(self, monkeypatch):
        monkeypatch.setattr(conv, "event_dtype", None)
        monkeypatch.setattr(conv, "_import_error", ImportError("hftbacktest not installed"))
        with pytest.raises(RuntimeError, match="hftbacktest not installed"):
            conv.convert_jsonl_to_npz("x.jsonl", "out.npz")


# ---------------------------------------------------------------------------
# convert_jsonl_to_npz — event type handling
# ---------------------------------------------------------------------------


class TestConvertEventTypes:
    def test_bidask_only_bids_creates_one_event(self, patched_conv, tmp_path):
        rows = [{"type": "BidAsk", "ts": 1, "bids": [{"price": 10000, "volume": 5}], "asks": []}]
        inp = tmp_path / "in.jsonl"
        out = tmp_path / "out.npz"
        inp.write_text(json.dumps(rows[0]) + "\n")

        patched_conv.convert_jsonl_to_npz(str(inp), str(out), scale=10_000)
        data = np.load(out)["data"]

        assert data.shape[0] == 1
        assert round(float(data["px"][0]), 4) == 1.0

    def test_bidask_only_asks_creates_one_event(self, patched_conv, tmp_path):
        rows = [{"type": "BidAsk", "ts": 1, "bids": [], "asks": [{"price": 12000, "volume": 3}]}]
        inp = tmp_path / "in.jsonl"
        out = tmp_path / "out.npz"
        inp.write_text(json.dumps(rows[0]) + "\n")

        patched_conv.convert_jsonl_to_npz(str(inp), str(out), scale=10_000)
        data = np.load(out)["data"]

        assert data.shape[0] == 1
        assert round(float(data["px"][0]), 4) == 1.2

    def test_tick_event_uses_exch_ts_field(self, patched_conv, tmp_path):
        row = {"type": "Tick", "exch_ts": 9999, "price": 20000, "volume": 1}
        inp = tmp_path / "in.jsonl"
        out = tmp_path / "out.npz"
        inp.write_text(json.dumps(row) + "\n")

        patched_conv.convert_jsonl_to_npz(str(inp), str(out), scale=10_000)
        data = np.load(out)["data"]

        assert data["exch"][0] == 9999

    def test_tick_local_ts_defaults_to_exch_ts_when_missing(self, patched_conv, tmp_path):
        row = {"type": "Tick", "ts": 555, "price": 10000, "volume": 2}
        inp = tmp_path / "in.jsonl"
        out = tmp_path / "out.npz"
        inp.write_text(json.dumps(row) + "\n")

        patched_conv.convert_jsonl_to_npz(str(inp), str(out), scale=10_000)
        data = np.load(out)["data"]

        assert data["exch"][0] == data["local"][0]

    def test_local_ts_used_when_provided(self, patched_conv, tmp_path):
        row = {"type": "Tick", "exch_ts": 1000, "local_ts": 2000, "price": 10000, "volume": 1}
        inp = tmp_path / "in.jsonl"
        out = tmp_path / "out.npz"
        inp.write_text(json.dumps(row) + "\n")

        patched_conv.convert_jsonl_to_npz(str(inp), str(out), scale=10_000)
        data = np.load(out)["data"]

        assert data["exch"][0] == 1000
        assert data["local"][0] == 2000

    def test_unsupported_event_type_is_skipped(self, patched_conv, tmp_path):
        rows = [
            {"type": "Unknown", "ts": 1},
            {"type": "Tick", "ts": 2, "price": 10000, "volume": 1},
        ]
        inp = tmp_path / "in.jsonl"
        out = tmp_path / "out.npz"
        inp.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

        patched_conv.convert_jsonl_to_npz(str(inp), str(out), scale=10_000)
        data = np.load(out)["data"]

        # Only the Tick row should be in the output
        assert data.shape[0] == 1


# ---------------------------------------------------------------------------
# convert_jsonl_to_npz — error / edge cases
# ---------------------------------------------------------------------------


class TestConvertEdgeCases:
    def test_empty_file_raises_value_error(self, patched_conv, tmp_path):
        inp = tmp_path / "empty.jsonl"
        out = tmp_path / "out.npz"
        inp.write_text("")

        with pytest.raises(ValueError, match="No events converted"):
            patched_conv.convert_jsonl_to_npz(str(inp), str(out))

    def test_blank_lines_are_skipped(self, patched_conv, tmp_path):
        row = {"type": "Tick", "ts": 10, "price": 10000, "volume": 1}
        inp = tmp_path / "in.jsonl"
        out = tmp_path / "out.npz"
        # intersperse blank lines
        inp.write_text("\n\n" + json.dumps(row) + "\n\n")

        patched_conv.convert_jsonl_to_npz(str(inp), str(out), scale=10_000)
        data = np.load(out)["data"]

        assert data.shape[0] == 1

    def test_malformed_json_line_is_skipped(self, patched_conv, tmp_path):
        row = {"type": "Tick", "ts": 10, "price": 10000, "volume": 1}
        inp = tmp_path / "in.jsonl"
        out = tmp_path / "out.npz"
        inp.write_text("{this is not json}\n" + json.dumps(row) + "\n")

        patched_conv.convert_jsonl_to_npz(str(inp), str(out), scale=10_000)
        data = np.load(out)["data"]

        # Malformed line skipped; only valid Tick row remains
        assert data.shape[0] == 1

    def test_output_parent_dir_created_if_missing(self, patched_conv, tmp_path):
        row = {"type": "Tick", "ts": 1, "price": 10000, "volume": 1}
        inp = tmp_path / "in.jsonl"
        out = tmp_path / "subdir" / "nested" / "out.npz"
        inp.write_text(json.dumps(row) + "\n")

        # Should not raise even though parent dirs don't exist yet
        patched_conv.convert_jsonl_to_npz(str(inp), str(out), scale=10_000)

        assert out.exists()

    def test_all_only_unsupported_types_raises_value_error(self, patched_conv, tmp_path):
        rows = [{"type": "OrderBook", "ts": 1}, {"type": "Status", "ts": 2}]
        inp = tmp_path / "in.jsonl"
        out = tmp_path / "out.npz"
        inp.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

        with pytest.raises(ValueError, match="No events converted"):
            patched_conv.convert_jsonl_to_npz(str(inp), str(out))

    def test_bidask_missing_bids_and_asks_produces_no_events_raises(self, patched_conv, tmp_path):
        row = {"type": "BidAsk", "ts": 1, "bids": [], "asks": []}
        inp = tmp_path / "in.jsonl"
        out = tmp_path / "out.npz"
        inp.write_text(json.dumps(row) + "\n")

        with pytest.raises(ValueError, match="No events converted"):
            patched_conv.convert_jsonl_to_npz(str(inp), str(out))

    def test_price_scaling_with_custom_scale(self, patched_conv, tmp_path):
        row = {"type": "Tick", "ts": 1, "price": 100, "volume": 2}
        inp = tmp_path / "in.jsonl"
        out = tmp_path / "out.npz"
        inp.write_text(json.dumps(row) + "\n")

        patched_conv.convert_jsonl_to_npz(str(inp), str(out), scale=100)
        data = np.load(out)["data"]

        assert round(float(data["px"][0]), 6) == 1.0

    def test_tick_volume_stored_as_qty(self, patched_conv, tmp_path):
        row = {"type": "Tick", "ts": 1, "price": 10000, "volume": 42}
        inp = tmp_path / "in.jsonl"
        out = tmp_path / "out.npz"
        inp.write_text(json.dumps(row) + "\n")

        patched_conv.convert_jsonl_to_npz(str(inp), str(out), scale=10_000)
        data = np.load(out)["data"]

        assert float(data["qty"][0]) == 42.0

    def test_bidask_volume_stored_as_qty(self, patched_conv, tmp_path):
        row = {
            "type": "BidAsk",
            "ts": 1,
            "bids": [{"price": 10000, "volume": 7}],
            "asks": [],
        }
        inp = tmp_path / "in.jsonl"
        out = tmp_path / "out.npz"
        inp.write_text(json.dumps(row) + "\n")

        patched_conv.convert_jsonl_to_npz(str(inp), str(out), scale=10_000)
        data = np.load(out)["data"]

        assert float(data["qty"][0]) == 7.0
