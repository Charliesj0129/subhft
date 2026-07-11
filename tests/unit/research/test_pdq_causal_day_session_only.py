"""Day-session mask boundaries (08:45-13:45 Taipei time, inclusive/exclusive)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from research.tools.pdq_causal_day_session_only import day_session_mask


def _tpe(hour: int, minute: int, day: int = 5) -> int:
    # Asia/Taipei is UTC+8 year-round (no DST), so a fixed offset is exact.
    dt = datetime(2026, 3, day, hour, minute, tzinfo=timezone.utc)
    return int(dt.timestamp()) - 8 * 3600


def test_day_session_mask_includes_open_and_excludes_close() -> None:
    seconds = np.array(
        [
            _tpe(8, 44),  # 1 minute before open: excluded
            _tpe(8, 45),  # exact open: included
            _tpe(11, 0),  # midday: included
            _tpe(13, 44),  # 1 minute before close: included
            _tpe(13, 45),  # exact close: excluded (day session already ended)
            _tpe(15, 0),  # night session open: excluded
            _tpe(2, 0, day=6),  # deep night session: excluded
        ],
        dtype=np.int64,
    )

    mask = day_session_mask(seconds)

    np.testing.assert_array_equal(mask, [False, True, True, True, False, False, False])
