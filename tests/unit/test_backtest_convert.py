import json

import numpy as np

from hft_platform.backtest import convert as conv


def test_convert_jsonl_to_npz(tmp_path, monkeypatch):
    dtype = np.dtype(
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
    monkeypatch.setattr(conv, "event_dtype", dtype)
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
