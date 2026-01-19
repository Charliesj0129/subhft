from typing import Any, Dict

from hft_platform.features.advanced_liquidity import RollingAmihud, RollingEffectiveSpread
from hft_platform.features.entropy import earth_mover_distance, lob_entropy
from hft_platform.features.fractal import hurst_exponent

# Feature Engineering Modules
from hft_platform.features.micro_price import stoikov_micro_price

"""
Standard Library of Alpha Factors for HFT.
Optimized for calculation on standardized LOB dictionaries.
"""


def mid_price(lob: Dict[str, Any]) -> float:
    """Calculate mid price from LOB snapshot/dict."""
    if "mid_price" in lob:
        return lob["mid_price"]

    bids = lob.get("bids", [])
    asks = lob.get("asks", [])

    if not bids or not asks:
        return float("nan")

    # Assuming tuple format from feature modules or list of dicts
    # LOB format standardization is key here.
    # If list of lists/tuples: [[p,v], ...]
    # If list of dicts: [{"price":p, "volume":v}, ...]
    # We should detect or coerce.
    # The feature modules assumed list of lists (standard format from loader).
    # If existing code used dicts, we need to be careful.

    # Let's support both or standardized.
    # Currently existing code assumes [{"price":..}]
    # But new features assumed [[p, v]]

    # I will stick to what was there for backward compat, but new features might need converting.
    # Actually, let's just expose the new classes.

    best_bid = get_price(bids, 0)
    best_ask = get_price(asks, 0)
    return (best_bid + best_ask) / 2.0


def get_price(rows, level):
    if not rows or len(rows) <= level:
        return 0.0
    row = rows[level]
    if isinstance(row, dict):
        return row.get("price", 0.0)
    return row[0]


def micro_price(lob: Dict[str, Any]) -> float:
    """Wrapper for Stoikov Micro-Price."""
    # Convert dict rows to expected list of lists if needed?
    # stoikov_micro_price expects [[p,v]].
    # If lob has dicts, we need adapter.
    return stoikov_micro_price(normalize_lob(lob))


def normalize_lob(lob: Dict[str, Any]) -> Dict:
    """Helper to ensure LOB is compatible with feature modules ([[p,v]...])."""
    # Check first row
    bids = lob.get("bids", [])
    if bids and isinstance(bids[0], dict):
        return {
            "bids": [[b["price"], b["volume"]] for b in bids],
            "asks": [[a["price"], a["volume"]] for a in lob.get("asks", [])],
        }
    return lob


# --- Phase 5 Helpers ---


def price_entropy(lob: Dict[str, Any]) -> float:
    """Calculates Shannon Entropy of volume distribution."""
    return lob_entropy(normalize_lob(lob))


def get_emd(lob_prev: Dict[str, Any], lob_curr: Dict[str, Any]) -> float:
    """Calculates Earth Mover's Distance between two LOB snapshots (structure change)."""
    # Assuming we compare Bids distribution? Or combined?
    # entropy.earth_mover_distance takes two lists.
    # We need to extract distributions.
    # Simplified: Comparison of Total Volume Distribution across similar price bins.
    # This requires price alignment which is complex for EMD if bins shift.
    # entropy.py implementation assumed aligned bins.
    # For simplicity, we extract volume vectors directly if they are usually static levels (e.g. 5 levels).
    # If levels are dynamic (orders), alignment is needed.
    # LOB snapshots from loaders are usually depth-limited levels.
    # We warn user about alignment.
    lob1 = normalize_lob(lob_prev)
    lob2 = normalize_lob(lob_curr)

    # Extract volumes only (assuming levels correspond to relative depth 1..N)
    # This is "Depth Entropy" not "Price Entropy".
    v1 = [x[1] for x in lob1.get("bids", [])]
    v2 = [x[1] for x in lob2.get("bids", [])]

    return earth_mover_distance(v1, v2)


def get_hurst(time_series: list) -> float:
    """
    Calculates Hurst Exponent for a time-series.
    Input should be a list of floats (Prices or OFI values).
    """
    return hurst_exponent(time_series)


# Advanced Liquidity Wrappers need state. Users should instantiate classes.
# But we can provide factory.


def create_roll_estimator(window=100) -> RollingEffectiveSpread:
    return RollingEffectiveSpread(window)


def create_amihud_estimator(window=100) -> RollingAmihud:
    return RollingAmihud(window)
