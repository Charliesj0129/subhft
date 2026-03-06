from __future__ import annotations

import json
from pathlib import Path

from hft_platform.config.symbols import write_contract_cache


def test_write_contract_cache_writes_versioned_payload(tmp_path: Path):
    path = tmp_path / "contracts.json"
    write_contract_cache([{"code": "2330"}], str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "updated_at" in data
    assert data["contracts"][0]["code"] == "2330"
    v1 = data.get("cache_version", 0)
    write_contract_cache([{"code": "2317"}], str(path))
    data2 = json.loads(path.read_text(encoding="utf-8"))
    assert data2["contracts"][0]["code"] == "2317"
    assert int(data2.get("cache_version", 0)) >= int(v1)
