"""Flow heatmap generator for Market Analysis Reports.

Produces a PNG image showing order-flow direction (U/D ratio color strip),
volume bars, large-trade markers, and a price overlay for a single session.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from functools import lru_cache
from typing import TYPE_CHECKING

import structlog

from hft_platform.contracts.types import PLATFORM_SCALE

if TYPE_CHECKING:
    from hft_platform.reports.models import SessionData

logger = structlog.get_logger(__name__)

_SESSION_LABEL: dict[str, str] = {
    "day": "日",
    "night": "夜",
}
_CJK_FONT_CANDIDATES: tuple[str, ...] = (
    "Noto Sans CJK TC",
    "Noto Sans CJK SC",
    "Noto Sans TC",
    "Noto Sans SC",
    "Source Han Sans TW",
    "Source Han Sans CN",
    "Microsoft JhengHei",
    "PingFang TC",
    "Heiti TC",
    "SimHei",
    "WenQuanYi Zen Hei",
)


@lru_cache(maxsize=1)
def _resolve_title_style() -> tuple[dict[str, str], str | None, str]:
    """Return localized session labels and an optional CJK-capable title font.

    Many CI environments only ship DejaVu Sans, which cannot render the
    Chinese title text used in reports. When no known CJK font is present,
    fall back to an ASCII title to avoid repeated glyph-missing warnings.
    """
    try:
        from matplotlib import font_manager
    except ImportError:
        return {"day": "Day", "night": "Night"}, None, "Flow Heatmap"

    available_fonts = {font.name for font in font_manager.fontManager.ttflist}
    for candidate in _CJK_FONT_CANDIDATES:
        if candidate in available_fonts:
            return _SESSION_LABEL, candidate, "流向熱力圖"

    return {"day": "Day", "night": "Night"}, None, "Flow Heatmap"


def generate_heatmap(sd: SessionData) -> bytes | None:
    """Generate a flow heatmap PNG for the given session data.

    Returns PNG bytes, or ``None`` when data is empty or matplotlib is
    unavailable.
    """
    if not sd.flow_5m or not sd.bars_5m:
        return None

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.dates as mdates  # noqa: E402
        import matplotlib.pyplot as plt  # noqa: E402
        from matplotlib.cm import RdYlGn  # noqa: E402
        from matplotlib.colors import TwoSlopeNorm  # noqa: E402
    except ImportError:
        logger.warning("matplotlib not installed; skipping heatmap generation")
        return None

    # -- Parse timestamps and extract data vectors --------------------------
    times = [datetime.strptime(fb.ts.split(".")[0], "%Y-%m-%d %H:%M:%S") for fb in sd.flow_5m]
    ud_ratios = [fb.ud_ratio for fb in sd.flow_5m]
    volumes = [fb.total_vol for fb in sd.flow_5m]

    bar_times = [datetime.strptime(b.ts.split(".")[0], "%Y-%m-%d %H:%M:%S") for b in sd.bars_5m]
    mid_prices = [(b.high + b.low) / 2.0 / PLATFORM_SCALE for b in sd.bars_5m]

    max_vol = max(volumes) if volumes else 1
    norm_vols = [v / max_vol for v in volumes]

    # -- Colormap norm centered at 1.0 (neutral U/D ratio) -----------------
    vmin = min(ud_ratios) if ud_ratios else 0.5
    vmax = max(ud_ratios) if ud_ratios else 1.5
    # Ensure the norm boundaries are strictly around 1.0
    if vmin >= 1.0:
        vmin = 0.5
    if vmax <= 1.0:
        vmax = 1.5
    norm = TwoSlopeNorm(vmin=vmin, vcenter=1.0, vmax=vmax)

    # -- Build figure -------------------------------------------------------
    fig, ax = plt.subplots(figsize=(12, 5))
    ax2 = ax.twinx()

    # Color strip (bottom): U/D ratio heatmap as narrow horizontal bars
    bar_width = (times[1] - times[0]) if len(times) > 1 else timedelta(minutes=5)
    for t, ratio in zip(times, ud_ratios, strict=True):
        color = RdYlGn(norm(ratio))
        ax.bar(t, 0.08, width=bar_width, bottom=0, color=color, align="center", edgecolor="none")

    # Volume bars (semi-transparent gray)
    ax.bar(times, norm_vols, width=bar_width, bottom=0.1, color="gray", alpha=0.4, align="center", edgecolor="none")

    # Large trade markers
    for lt in sd.large_trades:
        lt_time = datetime.strptime(lt.ts.split(".")[0], "%Y-%m-%d %H:%M:%S")
        lt_price = lt.price / PLATFORM_SCALE
        marker = "^" if lt.direction == "buy" else ("v" if lt.direction == "sell" else "o")
        color = "green" if lt.direction == "buy" else ("red" if lt.direction == "sell" else "gray")
        size = max(30, min(200, lt.volume * 2))
        ax2.scatter(lt_time, lt_price, marker=marker, s=size, color=color, zorder=5, edgecolors="black", linewidths=0.5)

    # Price line on right Y axis
    if bar_times and mid_prices:
        ax2.plot(bar_times, mid_prices, color="royalblue", linewidth=1.5, label="Mid Price")
        ax2.set_ylabel("Price")

    # Formatting
    ax.set_ylim(0, 1.2)
    ax.set_yticks([])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_xlabel("Time")

    session_labels, title_font, title_suffix = _resolve_title_style()
    session_label = session_labels.get(sd.session, sd.session)
    if title_font is None:
        ax.set_title(f"{sd.symbol} {session_label} {sd.date} {title_suffix}")
    else:
        ax.set_title(
            f"{sd.symbol} {session_label}\u76e4 {sd.date} {title_suffix}",
            fontname=title_font,
        )

    fig.tight_layout()

    # -- Write to bytes -----------------------------------------------------
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
