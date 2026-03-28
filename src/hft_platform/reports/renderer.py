"""ReportRenderer — formats ScenarioReport into Telegram-ready message lists.

Free tier:  [summary, flow_brief, disclaimer]         = 3 messages
Paid tier:  [summary, flow_detail, levels, scenarios, disclaimer] = 5 messages

All prices are scaled int x10_000 per the platform Precision Law.
"""

from __future__ import annotations

from typing import Final

from hft_platform.reports.models import ScenarioReport

__all__ = ["ReportRenderer", "_p", "_pct", "_stars", "_ud_bar"]

PLATFORM_SCALE: Final[int] = 10_000
TELEGRAM_MAX_LEN: Final[int] = 4096

SESSION_LABELS: Final[dict[str, str]] = {
    "day": "日盤",
    "night": "夜盤",
}


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _p(scaled: int) -> str:
    """Convert scaled price (int x10_000) to human-readable string with commas."""
    return f"{scaled // PLATFORM_SCALE:,}"


def _pct(open_p: int, close_p: int) -> str:
    """Return change string like '▼611 (-1.85%)' or '▲100 (+0.30%)'."""
    diff = close_p - open_p
    diff_pts = diff // PLATFORM_SCALE
    pct = diff / open_p * 100 if open_p else 0.0
    arrow = "▲" if diff >= 0 else "▼"
    sign = "+" if diff >= 0 else ""
    return f"{arrow}{abs(diff_pts):,} ({sign}{pct:.2f}%)"


def _stars(n: int) -> str:
    """Return star string for importance 1-3."""
    filled = min(max(n, 0), 3)
    return "★" * filled + "☆" * (3 - filled)


def _ud_bar(ratio: float) -> str:
    """Return a short textual bar representing U/D ratio."""
    if ratio >= 2.0:
        return "█████ 強多"
    if ratio >= 1.5:
        return "████░ 多"
    if ratio >= 1.1:
        return "███░░ 偏多"
    if ratio >= 0.9:
        return "██░░░ 平衡"
    if ratio >= 0.67:
        return "█░░░░ 偏空"
    if ratio >= 0.5:
        return "▌░░░░ 空"
    return "░░░░░ 強空"


# ---------------------------------------------------------------------------
# Median helper
# ---------------------------------------------------------------------------


def _median_spread(spread_dist: dict[int, int]) -> int:
    """Return weighted median spread in pts from spread_dist."""
    if not spread_dist:
        return 0
    total = sum(spread_dist.values())
    mid = total / 2
    cumulative = 0
    for pts in sorted(spread_dist):
        cumulative += spread_dist[pts]
        if cumulative >= mid:
            return pts
    return 0


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------


def _build_summary(report: ScenarioReport) -> str:
    sd = report.signal.session_data
    label = SESSION_LABELS.get(sd.session, sd.session)
    change = _pct(sd.open, sd.close)
    median_pts = _median_spread(sd.spread_dist)
    return (
        f"📊 台指期{label}報告 {sd.date}\n"
        f"\n"
        f"{sd.symbol}  {_p(sd.open)} → {_p(sd.close)}  {change}\n"
        f"High {_p(sd.high)} | Low {_p(sd.low)} | Vol {sd.volume:,}\n"
        f"Ticks {sd.tick_count:,}\n"
        f"Spread 中位數: {median_pts}pts"
    )


def _build_flow_brief(report: ScenarioReport) -> str:
    sig = report.signal
    large_dir = "空方" if sig.large_net <= 0 else "多方"
    return (
        f"🔍 知情流摘要\n"
        f"\n"
        f"方向: {report.direction} {report.confidence_pct}%\n"
        f"全場淨流: {sig.total_net_flow:+,} 口\n"
        f"大單淨方向: {large_dir} {abs(sig.large_net):,} 口\n"
        f"\n"
        f"#台指期 #盤後分析"
    )


def _build_flow_detail(report: ScenarioReport) -> str:
    sig = report.signal
    ss = sig.strongest_sell
    sb = sig.strongest_buy

    lines = [
        "🔍 知情流分析",
        "",
        f"▎全場 U/D = {sig.ud_ratio_session:.3f}  淨流 {sig.total_net_flow:+,} 口",
        f"▎最強空方: {ss.ts} U/D={ss.ud_ratio:.3f} net={ss.net_flow:+,}",
        f"▎最強多方: {sb.ts} U/D={sb.ud_ratio:.3f} net={sb.net_flow:+,}",
        "",
        "▎大單:",
        f"  🔴 賣方 ~{sig.large_sell_volume:,} 口  🟢 買方 ~{sig.large_buy_volume:,} 口",
    ]

    # Top 5 key trades
    if sig.key_large_trades:
        top5 = sig.key_large_trades[:5]
        for trade in top5:
            icon = "🔴" if trade.direction == "sell" else "🟢"
            lines.append(f"  {icon} {trade.volume:,}@{_p(trade.price)}")

    # Time-of-day U/D breakdown using flow_5m in ~2-hour chunks (24 bars each)
    flow_bars = sig.session_data.flow_5m
    if flow_bars:
        lines.append("")
        lines.append("▎時段 U/D:")
        chunk_size = max(1, len(flow_bars) // 5) if len(flow_bars) >= 5 else len(flow_bars)
        chunks: list[list] = []
        for i in range(0, len(flow_bars), chunk_size):
            chunk = flow_bars[i : i + chunk_size]
            if chunk:
                chunks.append(chunk)
        # Limit to first 5 chunks to keep message concise
        for chunk in chunks[:5]:
            avg_ud = sum(b.ud_ratio for b in chunk) / len(chunk)
            start_ts = chunk[0].ts[11:16]  # HH:MM
            lines.append(f"  {start_ts}  {_ud_bar(avg_ud)}  ({avg_ud:.3f})")

    return "\n".join(lines)


def _build_levels(report: ScenarioReport) -> str:
    sig = report.signal
    lines = ["🎯 關鍵點位", "", "▎支撐:"]

    for pl in sig.supports:
        # Match support to a key level label if available
        kl_match = next((k for k in report.key_levels if k.price == pl.price), None)
        label = kl_match.label if kl_match else "S"
        imp = kl_match.importance if kl_match else max(1, round(pl.strength * 3))
        lines.append(f"  {label} {_p(pl.price)}  {_stars(imp)}  {pl.reason}")

    lines.append("")
    lines.append("▎壓力:")
    for pl in sig.resistances:
        kl_match = next((k for k in report.key_levels if k.price == pl.price), None)
        label = kl_match.label if kl_match else "R"
        imp = kl_match.importance if kl_match else max(1, round(pl.strength * 3))
        lines.append(f"  {label} {_p(pl.price)}  {_stars(imp)}  {pl.reason}")

    # Entry reference
    lines.append("")
    lines.append(f"▎進場參考 ({report.direction}):")
    lo, hi = report.entry_zone
    lines.append(f"  進場區  {_p(lo)}-{_p(hi)}")
    lines.append(f"  目標    {_p(report.target)}")
    lines.append(f"  止損    {_p(report.stop_loss)}")

    return "\n".join(lines)


def _build_scenarios(report: ScenarioReport) -> str:
    lines = ["📋 情境規劃"]
    labels = ["A", "B", "C", "D", "E"]
    for idx, sc in enumerate(report.scenarios):
        label = labels[idx] if idx < len(labels) else str(idx + 1)
        lines.append("")
        lines.append(f"【情境 {label}】{sc.label} — 機率{sc.probability}")
        lines.append(f"  {sc.condition}")
        if sc.target > 0:
            lines.append(f"  → 目標 {_p(sc.target)}")
        lines.append(f"  {sc.description}")
    return "\n".join(lines)


def _build_disclaimer() -> str:
    return "⚠️ 本報告基於歷史行情數據自動生成，\n僅供參考，不構成投資建議。\n投資有風險，請自行評估。"


# ---------------------------------------------------------------------------
# Public renderer
# ---------------------------------------------------------------------------


class ReportRenderer:
    """Renders a ScenarioReport into a list of Telegram-ready strings.

    All returned strings are ≤ TELEGRAM_MAX_LEN (4096) characters.
    """

    def render(self, report: ScenarioReport, tier: str) -> list[str]:
        """Return list of message strings for the given tier.

        Args:
            report: The fully populated ScenarioReport.
            tier:   "free" or "paid".

        Returns:
            3-element list for "free", 5-element list for "paid".
        """
        summary = _build_summary(report)
        disclaimer = _build_disclaimer()

        if tier == "paid":
            messages = [
                summary,
                _build_flow_detail(report),
                _build_levels(report),
                _build_scenarios(report),
                disclaimer,
            ]
        else:
            messages = [
                summary,
                _build_flow_brief(report),
                disclaimer,
            ]

        # Safety truncation — should never be needed with well-formed data
        return [m[:TELEGRAM_MAX_LEN] for m in messages]
