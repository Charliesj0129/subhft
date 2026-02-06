import numpy as np


def hurst_exponent(ts_input) -> float:
    """
    Calculate the Hurst Exponent of a time series using R/S Analysis.
    H ~ 0.5: Random Walk (Geometric Brownian Motion).
    H > 0.5: Persistent (Trend).
    H < 0.5: Anti-persistent (Mean Reversion).

    Args:
        ts_input: List or Array of time series values.

    Returns:
        float: Hurst Exponent.
    """
    ts = np.array(ts_input)
    N = len(ts)
    if N < 20:
        return 0.5  # Not enough data

    # Create sub-windows sizes
    # We need range of lags to fit slope.
    # Powers of 2?
    min_chunk = 8
    max_chunk = N

    # Generate scales (m)
    scales: list[int] = []
    chunk_size = min_chunk
    while chunk_size <= max_chunk:
        scales.append(chunk_size)
        chunk_size *= 2

    if len(scales) < 3:
        # Fallback to simple R/S for single window if simplistic approximation needed,
        # but Hurst is scaling property.
        # Let's try simpler logic if N is small: just return 0.5
        pass

    # Calculate R/S for each scale
    rs_values: list[float] = []

    for m in scales:
        # Split into N/m chunks
        # Ignore remainder
        num_chunks = N // m

        chunk_rs: list[float] = []
        for i in range(num_chunks):
            start = i * m
            end = start + m
            chunk = ts[start:end]

            # R/S Calculation
            mean = np.mean(chunk)
            diff = chunk - mean
            z = np.cumsum(diff)  # Cumulative Deviate
            r = np.max(z) - np.min(z)  # Range
            s = np.std(chunk)  # Standard Deviation

            if s == 0:
                rs = 0.0  # Flat
            else:
                rs = r / s
            chunk_rs.append(rs)

        # Average R/S for this scale
        if chunk_rs:
            rs_values.append(float(np.mean(chunk_rs)))
        else:
            rs_values.append(0.0)

    # Remove zeros and log
    valid_scales: list[float] = []
    valid_rs: list[float] = []

    for m, rs in zip(scales, rs_values):
        if rs > 0:
            valid_scales.append(np.log10(m))
            valid_rs.append(np.log10(rs))

    if len(valid_scales) < 2:
        return 0.5

    # Linear Regression to find Slope H
    # log(R/S) = H * log(m) + c
    H, c = np.polyfit(valid_scales, valid_rs, 1)

    return H
