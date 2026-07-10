"""Monthly cost aggregation for the causal timeframe sweep."""

from __future__ import annotations

import pandas as pd

from research.tools.pdq_causal_timeframe_sweep import COSTS, monthly_from_paths


def test_monthly_from_paths_excludes_incomplete_events_and_applies_cost() -> None:
    paths = pd.DataFrame(
        {
            "day": ["2026-03-05", "2026-03-06", "2026-04-01", "2026-04-01"],
            "gross_pnl": [10.0, 20.0, float("nan"), 6.0],
        }
    )

    monthly = monthly_from_paths(paths, timeframe="1m")

    assert list(monthly["month"]) == ["2026-03", "2026-04"]
    march = monthly.loc[monthly["month"].eq("2026-03")].iloc[0]
    april = monthly.loc[monthly["month"].eq("2026-04")].iloc[0]

    assert march["n"] == 2
    assert march["active_days"] == 2
    assert march["gross_mean"] == 15.0
    # The NaN row must not count toward April's n or mean.
    assert april["n"] == 1
    assert april["gross_mean"] == 6.0
    for cost in COSTS:
        assert march[f"net_mean_cost{int(cost)}"] == 15.0 - cost
    assert (monthly["timeframe"] == "1m").all()
