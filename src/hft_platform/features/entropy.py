import math
from typing import List


def shannon_entropy(distribution: List[float]) -> float:
    """
    Calculate Shannon Entropy H = -Sum(p * log2(p)).
    Args:
        distribution: List of probabilities (must sum to 1).
    """
    entropy = 0.0
    for p in distribution:
        if p > 0:
            entropy -= p * math.log2(p)
    return entropy


def lob_entropy(lob: dict, depth: int = 10) -> float:
    """
    Calculate entropy of volume distribution across price levels.
    Treats the LOB as a probability distribution of liquidity.
    Concentrated liquidity = Low Entropy.
    Dispersed liquidity = High Entropy.

    Considers both Bids and Asks as a single distribution or separate?
    Report says: "Liquidity distribution".
    Let's compute total volume entropy.
    """
    bids = lob.get("bids", [])
    asks = lob.get("asks", [])

    # Collect volumes
    volumes: List[float] = []

    for i in range(min(depth, len(bids))):
        # Handle dict or list format
        row = bids[i]
        vol = row.get("volume") if isinstance(row, dict) else row[1]
        if vol is not None:
            volumes.append(float(vol))

    for i in range(min(depth, len(asks))):
        row = asks[i]
        vol = row.get("volume") if isinstance(row, dict) else row[1]
        if vol is not None:
            volumes.append(float(vol))

    total_vol = sum(volumes)
    if total_vol == 0:
        return 0.0

    # Normalize
    probs = [v / total_vol for v in volumes]
    return shannon_entropy(probs)


def earth_mover_distance(dist1: List[float], dist2: List[float]) -> float:
    """
    Calculate Earth Mover's Distance (Wasserstein) between two 1D distributions.
    For 1D, EMD = Sum(|CDF1(i) - CDF2(i)|).
    Assumes dist1 and dist2 are aligned (same bins/levels).
    """
    if len(dist1) != len(dist2):
        raise ValueError("Distributions must have same length")

    cdf1 = 0.0
    cdf2 = 0.0
    emd = 0.0

    # Assuming histograms are normalized? EMD usually requires total mass to be equal.
    # If prob dists, mass is 1.

    for p1, p2 in zip(dist1, dist2):
        cdf1 += p1
        cdf2 += p2
        emd += abs(cdf1 - cdf2)

    return emd
